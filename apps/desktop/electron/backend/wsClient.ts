import crypto from 'node:crypto'
import http from 'node:http'
import type net from 'node:net'

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

const OPCODE_CONTINUATION = 0x0
const OPCODE_TEXT = 0x1
const OPCODE_CLOSE = 0x8
const OPCODE_PING = 0x9
const OPCODE_PONG = 0xa

export interface LoopbackWebSocketOptions {
  host: '127.0.0.1'
  port: number
  path: string
  token: string
  origin: string
  connectTimeoutMs?: number
  onMessage?: (payload: string) => void
  onClose?: (code: number, reason: string) => void
  onError?: (error: Error) => void
}

/**
 * Minimal RFC 6455 WebSocket client over Node's built-in `node:http` module.
 *
 * Used inside Electron Main (Node 20.16, where global WebSocket is unavailable
 * without adding third-party packages) so Bearer tokens and Origin headers are
 * sent on the HTTP Upgrade handshake without exposing credentials to the Renderer.
 */
export class LoopbackWebSocketClient {
  private readonly options: LoopbackWebSocketOptions
  private socket: net.Socket | null = null
  private buffer: Buffer = Buffer.alloc(0)
  private fragmentedChunks: Buffer[] = []
  private connected = false
  private closed = false

  constructor(options: LoopbackWebSocketOptions) {
    this.options = options
  }

  get isConnected(): boolean {
    return this.connected && !this.closed
  }

  connect(): Promise<void> {
    if (this.connected) {
      return Promise.resolve()
    }
    this.closed = false

    return new Promise((resolve, reject) => {
      const wsKey = crypto.randomBytes(16).toString('base64')
      const expectedAccept = crypto
        .createHash('sha1')
        .update(`${wsKey}${WS_GUID}`)
        .digest('base64')

      let settled = false
      const timeoutMs = this.options.connectTimeoutMs ?? 5000

      const req = http.request({
        hostname: this.options.host,
        port: this.options.port,
        path: this.options.path,
        method: 'GET',
        headers: {
          Connection: 'Upgrade',
          Upgrade: 'websocket',
          'Sec-WebSocket-Key': wsKey,
          'Sec-WebSocket-Version': '13',
          Authorization: `Bearer ${this.options.token}`,
          Origin: this.options.origin,
        },
      })

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true
          req.destroy()
          reject(new Error(`WebSocket connection timed out after ${timeoutMs}ms.`))
        }
      }, timeoutMs)

      req.on('upgrade', (res, socket, head) => {
        clearTimeout(timer)
        const acceptHeader = res.headers['sec-websocket-accept']
        if (acceptHeader !== expectedAccept) {
          socket.destroy()
          if (!settled) {
            settled = true
            reject(new Error('Invalid Sec-WebSocket-Accept header from backend.'))
          }
          return
        }

        this.socket = socket
        this.connected = true
        this.buffer = head.length > 0 ? Buffer.from(head) : Buffer.alloc(0)

        socket.on('data', (chunk: Buffer) => {
          this.handleData(chunk)
        })

        socket.on('error', (err: Error) => {
          this.options.onError?.(err)
        })

        socket.on('close', () => {
          const wasConnected = this.connected
          this.connected = false
          this.socket = null
          if (!this.closed) {
            this.closed = true
            if (wasConnected) {
              this.options.onClose?.(1006, 'Connection closed')
            }
          }
        })

        if (!settled) {
          settled = true
          resolve()
        }

        if (this.buffer.length > 0) {
          this.drainFrames()
        }
      })

      req.on('response', (res) => {
        clearTimeout(timer)
        res.resume()
        if (!settled) {
          settled = true
          reject(
            new Error(
              `WebSocket upgrade rejected with HTTP ${res.statusCode ?? 'unknown'}.`,
            ),
          )
        }
      })

      req.on('error', (err) => {
        clearTimeout(timer)
        if (!settled) {
          settled = true
          reject(err)
        }
      })

      req.end()
    })
  }

  sendText(text: string): void {
    if (!this.socket || !this.connected || this.closed) {
      throw new Error('WebSocket is not connected.')
    }
    const payload = Buffer.from(text, 'utf8')
    this.socket.write(this.encodeClientFrame(OPCODE_TEXT, payload))
  }

  sendJson(value: unknown): void {
    this.sendText(JSON.stringify(value))
  }

  close(code = 1000, reason = 'Normal closure'): void {
    if (this.closed) {
      return
    }
    this.closed = true
    this.connected = false

    const sock = this.socket
    this.socket = null
    if (!sock) {
      return
    }

    try {
      const reasonBytes = Buffer.from(reason, 'utf8')
      const payload = Buffer.alloc(2 + reasonBytes.length)
      payload.writeUInt16BE(code, 0)
      reasonBytes.copy(payload, 2)
      sock.write(this.encodeClientFrame(OPCODE_CLOSE, payload))
      sock.end()
    } catch {
      sock.destroy()
    }
  }

  private encodeClientFrame(opcode: number, payload: Buffer): Buffer {
    const length = payload.length
    let headerLength = 2
    if (length >= 126 && length <= 65535) {
      headerLength = 4
    } else if (length > 65535) {
      headerLength = 10
    }

    const maskKey = crypto.randomBytes(4)
    const frame = Buffer.alloc(headerLength + 4 + length)
    frame[0] = 0x80 | (opcode & 0x0f) // FIN=1

    if (length < 126) {
      frame[1] = 0x80 | length // MASK=1
    } else if (length <= 65535) {
      frame[1] = 0x80 | 126
      frame.writeUInt16BE(length, 2)
    } else {
      frame[1] = 0x80 | 127
      frame.writeBigUInt64BE(BigInt(length), 2)
    }

    maskKey.copy(frame, headerLength)
    for (let i = 0; i < length; i += 1) {
      frame[headerLength + 4 + i] = payload[i] ^ maskKey[i % 4]
    }
    return frame
  }

  private handleData(chunk: Buffer): void {
    this.buffer = this.buffer.length === 0 ? chunk : Buffer.concat([this.buffer, chunk])
    this.drainFrames()
  }

  private drainFrames(): void {
    while (this.buffer.length >= 2) {
      const firstByte = this.buffer[0]
      const secondByte = this.buffer[1]
      const fin = (firstByte & 0x80) !== 0
      const opcode = firstByte & 0x0f
      const masked = (secondByte & 0x80) !== 0
      let payloadLength = secondByte & 0x7f
      let offset = 2

      if (payloadLength === 126) {
        if (this.buffer.length < 4) return
        payloadLength = this.buffer.readUInt16BE(2)
        offset = 4
      } else if (payloadLength === 127) {
        if (this.buffer.length < 10) return
        const bigLength = this.buffer.readBigUInt64BE(2)
        payloadLength = Number(bigLength)
        offset = 10
      }

      const maskLength = masked ? 4 : 0
      const frameEnd = offset + maskLength + payloadLength
      if (this.buffer.length < frameEnd) {
        return
      }

      let payload = this.buffer.subarray(offset + maskLength, frameEnd)
      if (masked) {
        const maskKey = this.buffer.subarray(offset, offset + 4)
        const unmasked = Buffer.alloc(payloadLength)
        for (let i = 0; i < payloadLength; i += 1) {
          unmasked[i] = payload[i] ^ maskKey[i % 4]
        }
        payload = unmasked
      } else {
        payload = Buffer.from(payload)
      }

      this.buffer = this.buffer.subarray(frameEnd)
      this.handleFrame(fin, opcode, payload)
    }
  }

  private handleFrame(fin: boolean, opcode: number, payload: Buffer): void {
    if (opcode === OPCODE_PING) {
      if (this.socket && this.connected) {
        this.socket.write(this.encodeClientFrame(OPCODE_PONG, payload))
      }
      return
    }

    if (opcode === OPCODE_PONG) {
      return
    }

    if (opcode === OPCODE_CLOSE) {
      const code = payload.length >= 2 ? payload.readUInt16BE(0) : 1000
      const reason = payload.length > 2 ? payload.subarray(2).toString('utf8') : ''
      this.close(code, reason)
      this.options.onClose?.(code, reason)
      return
    }

    if (opcode === OPCODE_TEXT || opcode === OPCODE_CONTINUATION) {
      this.fragmentedChunks.push(payload)
      if (fin) {
        const fullMessage = Buffer.concat(this.fragmentedChunks).toString('utf8')
        this.fragmentedChunks = []
        this.options.onMessage?.(fullMessage)
      }
    }
  }
}
