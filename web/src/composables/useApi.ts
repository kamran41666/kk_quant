import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

api.interceptors.request.use((config) => {
  const url = config.url ?? ''
  if (url.startsWith('/live') && typeof window !== 'undefined') {
    try {
      const token = window.sessionStorage.getItem('quant.operator.token')
      if (token) config.headers.set('X-Operator-Token', token)
    } catch {
      // Access to sessionStorage can be denied in privacy-restricted contexts;
      // the request proceeds and the server returns its normal 401 guidance.
    }
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    console.error('API error:', err.response?.data || err.message)
    return Promise.reject(err)
  }
)

export function useApi() {
  return { api }
}
