import React, { useState, useRef, useCallback } from 'react'
import Markdown from './Markdown'

/**
 * SSE 客户端。
 *
 * 用 fetch + ReadableStream 而不是 EventSource：EventSource 只支持 GET，
 * 而问答是 POST 带 body 的。解析 SSE 需要自己处理 `event:` / `data:` 分行，
 * 注意 data 里可能含中文，且分帧边界可能落在多字节字符中间，
 * 因此必须用 TextDecoder 的流式模式而不是一次性 decode。
 */
async function streamChat(question, { onDelta, onCitations, onDone, onError, signal }) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal,
  })
  if (!res.ok || !res.body) {
    onError?.(new Error(`HTTP ${res.status}`))
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let event = ''

  const handle = (payload) => {
    if (event === 'delta') {
      // delta 的 payload 现在是 JSON 字符串（见 ChatService#sendDelta 的注释）：
      // 直接把文本塞进 SSE 的 data 行，会让「以空格开头的帧」被客户端按规范
      // 剥掉分隔空格，markdown 的 `### 一、` 就变成 `###一、`，标题退化。
      // 解析失败时退回原样，保证后端未升级时也不白屏。
      let text = payload
      try {
        const v = JSON.parse(payload)
        text = typeof v === 'string' ? v : payload
      } catch { /* 后端未升级：按旧的裸文本处理 */ }
      onDelta?.(text)
    }
    else if (event === 'citations') {
      try { onCitations?.(JSON.parse(payload)) } catch { /* 忽略非法 JSON */ }
    } else if (event === 'done' || event === 'blocked') {
      try { onDone?.({ ...JSON.parse(payload), blocked: event === 'blocked' }) } catch { /* noop */ }
    } else if (event === 'error') {
      try { onError?.(new Error(JSON.parse(payload).message)) } catch { onError?.(new Error(payload)) }
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 以空行分帧；最后一段可能不完整，留在 buffer 等下次
    let idx
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      event = ''
      const dataLines = []
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
      }
      if (dataLines.length) handle(dataLines.join('\n'))
    }
  }
}

const SAMPLES = [
  '住院医疗险的等待期是多少天？',
  '住院医疗险要自己先垫多少钱？',
  '重疾险的犹豫期是多久？犹豫期内退保扣钱吗？',
  '重疾险的等待期是多久？',
  '住院医疗险和重疾险的等待期分别是多久？',
  '哪些情况保险公司不赔？',
  '这个产品能保证收益吗？',
  '这款保险的年化收益率是多少？',
]

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState({})
  const abortRef = useRef(null)
  const bottomRef = useRef(null)

  const scroll = () => bottomRef.current?.scrollIntoView({ behavior: 'smooth' })

  const send = useCallback(async (question) => {
    const q = (question ?? input).trim()
    if (!q || busy) return

    const id = Date.now()
    setInput('')
    setBusy(true)
    setMessages((m) => [...m, { id, q, a: '', citations: [], streaming: true }])
    scroll()

    const ac = new AbortController()
    abortRef.current = ac

    const patch = (fn) => setMessages((m) => m.map((x) => (x.id === id ? fn(x) : x)))

    try {
      await streamChat(q, {
        signal: ac.signal,
        onDelta: (t) => { patch((x) => ({ ...x, a: x.a + t })); scroll() },
        onCitations: (c) => patch((x) => ({ ...x, citations: c || [] })),
        onDone: (d) => {
          // 用定稿覆盖 delta 拼接结果。done.answer 是后端原样转发 Dify 的权威文本，
          // 任何分帧/转义环节的损耗都在这里被抹平。流式过程中已经渲染对了，
          // 覆盖不会闪；只有真的丢过字符时才会看到一次纠正。
          patch((x) => ({
            ...x,
            streaming: false,
            a: typeof d.answer === 'string' && d.answer ? d.answer : x.a,
            refused: !!d.refused,
            elapsed: d.elapsedMs,
            blocked: !!d.blocked,
          }))
          setBusy(false)
          abortRef.current = null
          scroll()
        },
        onError: (e) => {
          patch((x) => ({ ...x, streaming: false, error: String(e.message || e) }))
          setBusy(false)
          abortRef.current = null
        },
      })
    } catch (e) {
      if (e.name !== 'AbortError') {
        patch((x) => ({ ...x, streaming: false, error: String(e.message || e) }))
      } else {
        patch((x) => ({ ...x, streaming: false, aborted: true }))
      }
      setBusy(false)
      abortRef.current = null
    }
  }, [input, busy])

  const stop = () => abortRef.current?.abort()

  return (
    <div className="app">
      <header className="hdr">
        <h1>保险条款智能问答助手</h1>
        <p className="sub">RAG · Dify 编排 · Java 治理层 · 答案附溯源，答不准则拒答</p>
      </header>

      <div className="samples">
        {SAMPLES.map((s) => (
          <button key={s} className="chip" disabled={busy} onClick={() => send(s)}>{s}</button>
        ))}
      </div>

      <div className="msgs">
        {messages.length === 0 && (
          <div className="empty">
            试着问上面任意一个问题。带「重疾险 / 住院医疗险」的问题会被路由到对应知识库；
            问知识库外的内容（如收益率）会明确拒答而不是编造。
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className="msg">
            <div className="q">{m.q}</div>
            <div className="a">
              {m.a ? (
                <Markdown
                  text={m.a}
                  tail={m.streaming ? <span className="cursor">▍</span> : null}
                />
              ) : (
                m.streaming && <span className="cursor">▍</span>
              )}
              {!m.streaming && m.aborted && <div className="note">（已中断）</div>}
              {!m.streaming && m.error && <div className="note err">出错了：{m.error}</div>}
              {!m.streaming && m.refused && !m.blocked && (
                <div className="note refuse">未在条款中检索到相关信息，未作答。</div>
              )}
              {m.blocked && <div className="note refuse">该问题涉及合规红线，已拦截。</div>}
            </div>

            {!m.streaming && m.elapsed != null && (
              <div className="meta">
                <span className="tagx">耗时 {m.elapsed} ms</span>
                {m.citations?.length > 0 && <span className="tagx">引用 {m.citations.length} 条</span>}
              </div>
            )}

            {m.citations?.length > 0 && (
              <div className="cites">
                <div className="cites-h">引用来源（{m.citations.length}）</div>
                {m.citations.map((c, i) => (
                  <div key={i} className="cite">
                    <button
                      className="cite-h"
                      onClick={() => setExpanded((x) => ({ ...x, [i]: !x[i] }))}
                    >
                      <span className="tag">[{i + 1}]</span>
                      {c.documentName || '未知文档'}
                      {c.pageNumber != null
                        ? ` 第 ${c.pageNumber} 页`
                        : c.segmentPosition != null
                          ? ` 第 ${c.segmentPosition} 段`
                          : ''}
                      {c.score != null && <span className="score">{Number(c.score).toFixed(4)}</span>}
                      <span className="caret">{expanded[i] ? '▲' : '▼'}</span>
                    </button>
                    {expanded[i] && <pre className="cite-b">{c.snippet}</pre>}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="bar">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && send()}
          placeholder="输入你的问题，Enter 发送"
          disabled={busy}
        />
        {busy ? (
          <button className="btn stop" onClick={stop}>中断</button>
        ) : (
          <button className="btn" onClick={() => send()} disabled={!input.trim()}>发送</button>
        )}
      </div>
    </div>
  )
}