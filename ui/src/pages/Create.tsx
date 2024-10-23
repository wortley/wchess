import { throttle } from "lodash"
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { toast } from "react-toastify"
import { useAccount, useBalance } from "wagmi"
import { estimateFeesPerGas, writeContract } from "wagmi/actions"
import { abi } from "../abi"
import TermsModal from "../components/TermsModal"
import { config } from "../config"
import { chainId, COMMISSION_PERCENTAGE, MAX_GAS, SC_ADDRESS } from "../constants"
import { socket } from "../socket"
import { StartData } from "../types"
import { parsePOL, POLtoGBP, POLtoUSD } from "../utils/currency"

export default function Create() {
  const navigate = useNavigate()
  const [newGameId, setNewGameId] = useState("")
  const [timeControl, setTimeControl] = useState<number>(3)
  const [wagerAmount, setWagerAmount] = useState<number>(0) // in POL
  const [gasPrice, setGasPrice] = useState<bigint>(0n)
  const [showModal, setShowModal] = useState<boolean>(false)
  const [rounds, setRounds] = useState<number>(1)

  const [wagerAmountGBP, setWagerAmountGBP] = useState<number>(0)
  const [wagerAmountUSD, setWagerAmountUSD] = useState<number>(0)

  const { address, isConnected } = useAccount()
  const { data: balance } = useBalance({ address, chainId })

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const throttledConverter = useCallback(
    throttle((amount) => {
      POLtoGBP(amount).then((gbpAmount) => setWagerAmountGBP(gbpAmount))
      POLtoUSD(amount).then((usdAmount) => setWagerAmountUSD(usdAmount))
    }, 500),
    []
  )

  useEffect(() => {
    async function onGameId(gameId: string) {
      const wagerWei = parsePOL(wagerAmount.toString())
      const commissionWei = (wagerWei * BigInt(COMMISSION_PERCENTAGE)) / BigInt(100)

      try {
        const result = await writeContract(config, {
          abi,
          address: SC_ADDRESS,
          functionName: "createGame",
          value: wagerWei + commissionWei,
          gas: MAX_GAS,
          args: [gameId, wagerWei],
        })
        console.log("Transaction successful:", result)
        setNewGameId(gameId)
      } catch (err) {
        console.error("Transaction error:", err)
        toast.error((err as Error).message.split(".")[0])
      }
    }

    function onStart(data: StartData) {
      navigate("/play", {
        state: {
          colour: data.colour,
          timeRemaining: data.timeRemaining,
          round: data.round,
          totalRounds: data.totalRounds,
        },
      })
    }

    socket.on("gameId", onGameId)
    socket.on("start", onStart)

    return () => {
      socket.off("gameId", onGameId)
      socket.off("start", onStart)
    }
  }, [wagerAmount, navigate])

  useEffect(() => {
    async function fetchGasPrice() {
      if (isConnected) {
        const priceInfo = await estimateFeesPerGas(config, {
          chainId,
        }) // gets estimated gas price in wei
        setGasPrice(priceInfo.maxFeePerGas)
      }
    }
    fetchGasPrice()
  }, [isConnected])

  useEffect(() => {
    if (wagerAmount) {
      throttledConverter(wagerAmount) // uses CMC API so throttle
    }
  }, [wagerAmount, throttledConverter])

  function validateGameCreation() {
    if (!isConnected) return "Please connect your wallet."
    if (!gasPrice) return "Please wait for gas price to load."

    const wagerWei = parsePOL(wagerAmount.toString())
    const commissionWei = (wagerWei * BigInt(COMMISSION_PERCENTAGE)) / BigInt(100)

    if (wagerWei + commissionWei >= balance!.value - gasPrice) return "Insufficient MATIC balance."
    return 0
  }

  function onCreateGame() {
    const err = validateGameCreation()
    if (err) {
      toast.error(err)
      return
    }
    socket.emit("create", timeControl, wagerAmount, address, rounds)
  }

  function onCancelGame() {
    socket.emit("cancel")
  }

  const step = import.meta.env.PROD ? 1 : 0.1

  return (
    <>
      <div className="home-div">
        {!newGameId && (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              onCreateGame()
            }}
          >
            <h4>New game</h4>
            <label htmlFor="time-control">Time control:</label>
            <select id="time-control" value={timeControl} required onChange={(e) => setTimeControl(parseInt(e.currentTarget.value))}>
              {/* <option value={-1} disabled hidden></option> */}
              <option value={3}>3m Blitz</option>
              <option value={5}>5m Blitz</option>
              <option value={10}>10m Rapid</option>
              <option value={30}>30m Classical</option>
            </select>
            <label htmlFor="rounds">Rounds:</label>
            <input
              type="number"
              id="rounds"
              onKeyDown={(e) => e.preventDefault()}
              style={{ caretColor: "transparent" }}
              value={rounds}
              min={1}
              step={1}
              max={10}
              onChange={(e) => setRounds(parseInt(e.currentTarget.value))}
              required
            />
            <label htmlFor="wager-amount">Wager (POL):</label>
            <input type="number" id="wager-amount" required value={wagerAmount} min={step} step={step} max={100} onChange={(e) => setWagerAmount(parseFloat(e.currentTarget.value))} />
            <p>
              Wager: {wagerAmountUSD.toFixed(2)} USD / {wagerAmountGBP.toFixed(2)} GBP
            </p>
            <p>Gas price: {(Number(gasPrice) / 10 ** 9).toFixed(2)} Gwei</p>
            <p>Commission: {COMMISSION_PERCENTAGE}%</p>
            <div className="accept-terms-container">
              <input type="checkbox" id="accept-terms" required />
              <label htmlFor="accept-terms">
                I accept the{" "}
                <a href="#" onClick={() => setShowModal(true)}>
                  terms of use
                </a>
              </label>
            </div>
            <button type="submit">Generate code</button>
            <button
              onClick={() => {
                setTimeControl(-1)
                navigate("/")
              }}
            >
              Back
            </button>
          </form>
        )}
        {newGameId && (
          <>
            <p>Share this code with a friend to play against them. Once they join and accept the wager, the game will start.</p>
            <h4>{newGameId}</h4>
            <button onClick={async () => await navigator.clipboard.writeText(newGameId).then(() => toast.success("Code copied to clipboard."))}>Copy code</button>
            <button onClick={onCancelGame}>Cancel and cash out</button>
          </>
        )}
      </div>
      <TermsModal show={showModal} setShow={setShowModal} />
    </>
  )
}
