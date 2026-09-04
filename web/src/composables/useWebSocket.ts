import { onUnmounted, ref, type Ref } from 'vue'

interface SharedConnection {
  connected: Ref<boolean>
  lastMessage: Ref<unknown>
  socket: WebSocket | null
  reconnectTimer: number | undefined
  heartbeatTimer: number | undefined
  attempts: number
  subscribers: number
  shouldReconnect: boolean
}

const connections = new Map<string, SharedConnection>()

function createConnection(channel: string): SharedConnection {
  const state: SharedConnection = {
    connected: ref(false),
    lastMessage: ref(null),
    socket: null,
    reconnectTimer: undefined,
    heartbeatTimer: undefined,
    attempts: 0,
    subscribers: 0,
    shouldReconnect: true,
  }

  const clearTimers = () => {
    window.clearTimeout(state.reconnectTimer)
    window.clearInterval(state.heartbeatTimer)
    state.reconnectTimer = undefined
    state.heartbeatTimer = undefined
  }

  const connect = () => {
    if (!state.shouldReconnect || state.socket?.readyState === WebSocket.OPEN || state.socket?.readyState === WebSocket.CONNECTING) return
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/${channel}`)
    state.socket = socket

    socket.onopen = () => {
      state.connected.value = true
      state.attempts = 0
      window.clearInterval(state.heartbeatTimer)
      state.heartbeatTimer = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send('ping')
      }, 25_000)
    }
    socket.onmessage = event => {
      try {
        state.lastMessage.value = JSON.parse(event.data)
      } catch {
        state.lastMessage.value = event.data
      }
    }
    socket.onerror = () => socket.close()
    socket.onclose = () => {
      state.connected.value = false
      window.clearInterval(state.heartbeatTimer)
      state.heartbeatTimer = undefined
      state.socket = null
      if (!state.shouldReconnect || state.subscribers === 0) return
      const delay = Math.min(1_000 * 2 ** state.attempts, 30_000)
      state.attempts += 1
      state.reconnectTimer = window.setTimeout(connect, delay)
    }
  }

  Object.assign(state, { connect, clearTimers })
  connect()
  return state
}

export function useWebSocket(channel: string) {
  const state = connections.get(channel) ?? createConnection(channel)
  connections.set(channel, state)
  state.subscribers += 1

  onUnmounted(() => {
    state.subscribers = Math.max(0, state.subscribers - 1)
    if (state.subscribers > 0) return
    state.shouldReconnect = false
    window.clearTimeout(state.reconnectTimer)
    window.clearInterval(state.heartbeatTimer)
    state.socket?.close(1000, 'view unmounted')
    state.socket = null
    state.connected.value = false
    connections.delete(channel)
  })

  return { connected: state.connected, lastMessage: state.lastMessage }
}
