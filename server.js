const express = require('express')
const cors = require('cors')

const app = express()

app.use(express.json({ limit: '100kb' }))

const allowedOrigins = (process.env.ALLOWED_ORIGINS || '')
  .split(',')
  .map(x => x.trim())
  .filter(Boolean)

app.use(cors({
  origin(origin, callback) {
    // Render 健康检查、curl、服务端请求可能没有 origin，直接放行。
    if (!origin) return callback(null, true)
    // 未配置 ALLOWED_ORIGINS 时不做来源限制，便于首次部署测试。
    if (allowedOrigins.length === 0) return callback(null, true)
    if (allowedOrigins.includes(origin)) return callback(null, true)
    return callback(new Error('origin_not_allowed'))
  },
  methods: ['POST', 'OPTIONS'],
  allowedHeaders: ['Content-Type']
}))

const REAL_REPORT_URL = process.env.REAL_REPORT_URL
const REPORT_API_TOKEN = process.env.REPORT_API_TOKEN

function isNonNegativeNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isValidPayload(payload) {
  if (!payload || typeof payload !== 'object') return false

  if (!/^\d+$/.test(String(payload.userUid || ''))) return false
  if (!['local', 'manual', 'daily', 'temp'].includes(payload.taskType)) return false

  const numberFields = [
    'totalParsed',
    'submitted',
    'failed',
    'whitelistSkipped',
    'durationSeconds'
  ]

  for (const field of numberFields) {
    if (!isNonNegativeNumber(payload[field])) return false
  }

  if (!payload.timestamp || Number.isNaN(Date.parse(payload.timestamp))) return false

  return true
}

app.get('/health', (_req, res) => {
  res.json({ ok: true })
})

app.post('/api/report-proxy', async (req, res) => {
  try {
    if (!REAL_REPORT_URL || !REPORT_API_TOKEN) {
      return res.status(500).json({
        ok: false,
        error: 'missing_server_env'
      })
    }

    const payload = req.body

    if (!isValidPayload(payload)) {
      return res.status(400).json({
        ok: false,
        error: 'invalid_payload'
      })
    }

    const response = await fetch(REAL_REPORT_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${REPORT_API_TOKEN}`
      },
      body: JSON.stringify({
        ...payload,
        clientType: 'chrome-extension',
        receivedAt: new Date().toISOString()
      })
    })

    const text = await response.text().catch(() => '')

    if (!response.ok) {
      return res.status(response.status).send(text || 'upstream_report_failed')
    }

    if (text) return res.status(200).send(text)
    return res.status(200).json({ ok: true })
  } catch (error) {
    console.error('report proxy error:', error)
    return res.status(500).json({
      ok: false,
      error: 'server_error'
    })
  }
})

const port = process.env.PORT || 3000
app.listen(port, () => {
  console.log(`report proxy listening on ${port}`)
})
