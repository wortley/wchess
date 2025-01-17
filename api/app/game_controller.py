import asyncio
import json
import os
import random
import uuid
from logging import Logger

import aioredis
import app.utils as utils
from aioredis.client import Redis
from app.constants import BROADCAST_KEY, CONCURRENT_GAME_LIMIT, MAX_EMIT_RETRIES, MILLISECONDS_PER_MINUTE, VALID_N_ROUNDS_RANGE, VALID_TIME_CONTROLS, VALID_WAGER_RANGE
from app.exceptions import CustomException
from app.game_contract import GameContract
from app.game_registry import GameRegistry
from app.models import Colour, Event, Game, Outcome
from app.rmq import RMQConnectionManager
from chess import Board
from socketio.asyncio_server import AsyncServer


class GameController:

    def __init__(self, rmq: RMQConnectionManager, redis_client: Redis, sio: AsyncServer, gr: GameRegistry, contract: GameContract, logger: Logger):
        self.rmq = rmq
        self.redis_client = redis_client
        self.sio = sio
        self.gr = gr
        self.contract = contract
        self.logger = logger

    def _on_emit_done(self, task, event, sid, attempts):
        try:
            task.result()  #  raises exception if task failed
        except Exception as e:
            if attempts < MAX_EMIT_RETRIES:
                self.logger.error(f"Emit event failed with exception: {e}, retrying...")
                new_task = asyncio.create_task(self.sio.emit(event.name, event.data, to=sid))
                new_task.add_done_callback(lambda t, sid=sid: self._on_emit_done(t, event, sid, attempts + 1))
            else:
                self.logger.error(f"Emit event failed {MAX_EMIT_RETRIES} times, giving up")

    async def _init_listener(self, gid, sid):
        self.logger.info("Initialising listener for game " + gid + ", user " + sid + ", on worker ID " + str(os.getpid()))

        def on_message(_, __, ___, body):
            message = json.loads(body)
            event = Event(**message)
            task = asyncio.create_task(self.sio.emit(event.name, event.data, to=sid))
            task.add_done_callback(lambda t, sid=sid: self._on_emit_done(t, event, sid, 1))

        self.gr.add_game_ctag(gid, self.rmq.channel.basic_consume(queue=utils.get_queue_name(gid, sid), on_message_callback=on_message, auto_ack=True))

    async def get_game_by_gid(self, gid, sid):
        """Get game state from redis by game ID"""
        try:
            game = utils.deserialise_game_state(await self.redis_client.get(utils.get_redis_game_key(gid)))
        except aioredis.RedisError as exc:
            raise CustomException(f"Redis error: {exc}", sid)
        if not game:
            raise CustomException("Game not found", sid)
        return game

    async def get_game_by_sid(self, sid):
        """Get game state from redis by player ID"""
        gid = self.gr.get_gid(sid)
        game = await self.get_game_by_gid(gid, sid)
        return game, gid

    async def save_game(self, gid, game, _=None):
        """Save game state in Redis"""
        try:
            await self.redis_client.set(utils.get_redis_game_key(gid), utils.serialise_game_state(game))
        except aioredis.RedisError as exc:
            raise CustomException(f"Redis error: {exc}", emit_local=False, gid=gid)

    async def _validate_game_creation(self, sid, time_control, wager, n_rounds):
        # rate limiting
        games_inpr = 0
        async for _ in self.redis_client.scan_iter("game:*"):  # count games in progress
            games_inpr += 1
        if games_inpr >= CONCURRENT_GAME_LIMIT:
            raise CustomException("Server at capacity. Please come back later", sid)

        # check wager meets min/max requirements
        if wager not in range(VALID_WAGER_RANGE[0], VALID_WAGER_RANGE[1] + 1):
            raise CustomException(f"Invalid wager. Wager must be in range {VALID_WAGER_RANGE} POL", sid)

        # check time control
        if time_control not in VALID_TIME_CONTROLS:
            raise CustomException("Invalid time control", sid)

        # check number of rounds
        if n_rounds not in range(VALID_N_ROUNDS_RANGE[0], VALID_N_ROUNDS_RANGE[1] + 1):
            raise CustomException(f"Number of rounds must be in range {VALID_N_ROUNDS_RANGE}", sid)

    def _validate_joining_gid(self, gid):
        try:
            uuid.UUID(gid)
        except ValueError:  # invalid UUID
            raise CustomException("Invalid game code")

    async def create(self, sid, time_control, wager, wallet_addr, n_rounds):
        """
        Create a new game

        :param sid: player's socket ID
        :param time_control: time control in minutes
        :param wager: wager amount (POL)
        :param wallet_addr: player's wallet address
        :param n_rounds: number of rounds in the game
        """
        await self._validate_game_creation(sid, time_control, wager, n_rounds)

        gid = str(uuid.uuid4())  # generate game ID
        self.sio.enter_room(sid, gid)  # create an SIO room for the game

        tr = time_control * MILLISECONDS_PER_MINUTE

        game = Game(
            players=[sid],
            board=Board(),
            wager=wager,
            player_wallet_addrs={sid: wallet_addr},
            time_control=time_control,
            match_score={sid: 0},
            n_rounds=n_rounds,
            round=1,
            tr_white=tr,
            tr_black=tr,
        )

        self.gr.add_player_gid_record(sid, gid)
        await self.save_game(gid, game, sid)

        # send game id to client
        await self.sio.emit("gameId", gid, to=sid)  # N.B no need to publish this to MQ

        # create fanout exchange for game
        self.rmq.channel.exchange_declare(exchange=gid, exchange_type="topic")
        # create player 1 queue
        self.rmq.channel.queue_declare(queue=utils.get_queue_name(gid, sid))
        # bind the queue to the game exchange
        self.rmq.channel.queue_bind(exchange=gid, queue=utils.get_queue_name(gid, sid), routing_key=sid)
        self.rmq.channel.queue_bind(exchange=gid, queue=utils.get_queue_name(gid, sid), routing_key=BROADCAST_KEY)

        # init listener
        await self._init_listener(gid, sid)

    async def get_game_details(self, sid, gid):
        """
        User request to view game information before joining

        Returns game information (time control and wager amount)
          - gives user joining game a chance to review and accept wager amount before joining
          - this flow also allows for us to check other player has sufficient POL balance

        :param sid: player's socket ID
        :param gid: game ID
        """
        self._validate_joining_gid(gid)

        game = await self.get_game_by_gid(gid, sid)
        if len(game.players) >= 2:
            raise CustomException("This game already has two players", sid)

        game_info = {
            "wagerAmount": game.wager,
            "timeControl": game.time_control,
            "totalRounds": game.n_rounds,
        }
        await self.sio.emit("gameInfo", game_info, to=sid)

    async def cancel_game(self, sid, created_on_contract):
        """
        Cancel game
          - for when game creator wishes to cancel the game and cash out (must be done before an opponent has joined the game)

        :param sid: player's socket ID
        :param created_on_contract: whether the contract interaction to create the game completed
        """
        game, gid = await self.get_game_by_sid(sid)
        if created_on_contract:
            await self.contract.cancel_game(gid)
        await self.sio.emit("gameCancelled", to=sid)
        await self.clear_game(sid, game, gid)

    async def accept_game(self, sid, gid, wallet_addr):
        """
        Accept game
          - for when user has reviewed game info and is ready to start

        :param sid: player's socket ID
        :param gid: game ID
        :param wallet_addr: player's wallet address
        """
        game = await self.get_game_by_gid(gid, sid)

        self.sio.enter_room(sid, gid)  # join room
        game.players.append(sid)
        game.player_wallet_addrs[sid] = wallet_addr
        game.match_score[sid] = 0

        self.gr.add_player_gid_record(sid, gid)

        # randomly pick white and black
        random.shuffle(game.players)

        # create player 2 queue
        self.rmq.channel.queue_declare(queue=utils.get_queue_name(gid, sid))
        # bind the queue to the game exchange
        self.rmq.channel.queue_bind(exchange=gid, queue=utils.get_queue_name(gid, sid), routing_key=sid)
        self.rmq.channel.queue_bind(exchange=gid, queue=utils.get_queue_name(gid, sid), routing_key=BROADCAST_KEY)

        await self._init_listener(gid, sid)

        # set start timestamp (ms) and save game before sending start events
        game.last_turn_timestamp = utils.get_time_now_ms()
        await self.save_game(gid, game, sid)

        # send start events to both players
        for i, colour in enumerate([Colour.BLACK.value[0], Colour.WHITE.value[0]]):
            utils.publish_event(
                self.rmq.channel,
                gid,
                Event(
                    "start",
                    {"colour": colour, "timeRemaining": game.tr_white, "round": game.round, "totalRounds": game.n_rounds},
                ),
                game.players[i],
            )

        # update usage stats
        await self.redis_client.incr(utils.get_redis_stat_key("n_games"))
        await self.redis_client.incr(utils.get_redis_stat_key("total_wagered"), game.wager * 2)

    async def handle_end_of_round(self, gid: str, game: Game):
        overall_winner = None
        match_score = game.match_score
        if game.round == game.n_rounds:
            # end of match
            if match_score[game.players[0]] > match_score[game.players[1]]:  # player who had black in last round wins overall
                overall_winner = 0
            elif match_score[game.players[0]] < match_score[game.players[1]]:  # player who had white in last round wins overall
                overall_winner = 1

            # publish matchEnded event
            utils.publish_event(self.rmq.channel, gid, Event("matchEnded", {"overallWinner": overall_winner}))
            # save game
            game.finished = True
            await self.save_game(gid, game)

            # declare result on SC
            if overall_winner is not None:
                await self.contract.declare_winner(gid, game.player_wallet_addrs[game.players[overall_winner]])
            else:  # draw
                await self.contract.declare_draw(gid)
        else:
            # start next round
            await asyncio.sleep(15)  # wait some time before starting next round
            game = await self.get_game_by_gid(gid, game.players[0])  # refresh game in memory
            game.round += 1
            game.match_score = match_score  # restore match score
            game.board.reset()  # reset board
            game.players.reverse()  # switch white and black
            game.tr_white = game.tr_black = game.time_control * MILLISECONDS_PER_MINUTE
            game.last_turn_timestamp = utils.get_time_now_ms()

            await self.save_game(gid, game)

            if not game.finished:  # if game has not been abandoned, send start event
                for i, colour in enumerate([Colour.BLACK.value[0], Colour.WHITE.value[0]]):
                    utils.publish_event(
                        self.rmq.channel,
                        gid,
                        Event(
                            "start",
                            {"colour": colour, "timeRemaining": game.tr_white, "round": game.round, "totalRounds": game.n_rounds},
                        ),
                        game.players[i],
                    )

    async def handle_exit(self, sid):
        if not self.gr.get_gid(sid):
            # if player already removed from game or game deleted, return
            return

        game, gid = await self.get_game_by_sid(sid)
        if len(game.players) > 1 and not game.finished:
            # if game not finished, the player automatically loses the match
            winner_ind = utils.opponent_ind(game.players.index(sid))
            utils.publish_event(self.rmq.channel, gid, Event("move", {"winner": winner_ind, "outcome": Outcome.ABANDONED.value, "matchScore": game.match_score}))
            utils.publish_event(self.rmq.channel, gid, Event("matchEnded", {"overallWinner": winner_ind}))
            game.finished = True
            await self.save_game(gid, game)
            await self.contract.declare_winner(gid, game.player_wallet_addrs[game.players[winner_ind]])

        await self.clear_game(sid, game, gid)

    async def clear_game(self, sid, game, gid):
        """Clears a user's game(s) from memory"""
        self.logger.info("Clearing game " + gid + " (user " + sid + ")")
        self.gr.remove_player_gid_record(sid)
        self.rmq.channel.queue_unbind(utils.get_queue_name(gid, sid), exchange=gid, routing_key=sid)
        self.rmq.channel.queue_unbind(utils.get_queue_name(gid, sid), exchange=gid, routing_key=BROADCAST_KEY)
        self.sio.leave_room(sid, gid)

        if len(game.players) > 1:  # remove player from game.players
            game.players.remove(sid)
            await self.save_game(gid, game, sid)
        else:  # last player to leave game
            await self.sio.close_room(gid)
            for ctag in self.gr.get_game_ctags(gid):
                self.rmq.channel.basic_cancel(consumer_tag=ctag)
            self.gr.remove_all_game_ctags(gid)
            self.rmq.channel.exchange_delete(exchange=gid)
            await self.redis_client.delete(utils.get_redis_game_key(gid))
