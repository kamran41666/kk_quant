import { ref, onUnmounted } from 'vue'

export function useWebSocket(channel: string) {
  const connected = ref(false)
  const lastMessage = ref<any>(null)
  let ws: WebSocket | null = null

  function connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${protocol}://${window.location.host}/ws/${channel}`)

    ws.onopen = () => { connected.value = true }
    ws.onclose = () => { connected.value = false; setTimeout(connect, 3000) }
    ws.onmessage = (event) => {
      try { lastMessage.value = JSON.parse(event.data) } catch { lastMessage.value = event.data }
    }
  }

  connect()

  onUnmounted(() => {
    ws?.close()
  })

  return { connected, lastMessage }
}
