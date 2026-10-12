import React from 'react'

/**
 * 极简 Markdown 渲染器。
 *
 * 为什么自己写而不用 marked / react-markdown：
 *   1. 模型输出是**不可信输入**。主流 markdown 库默认把 HTML 塞进
 *      dangerouslySetInnerHTML，这里一律转成 React 元素、由 React 自己转义，
 *      没有 XSS 面。
 *   2. 答案由 SSE 增量到达，每一帧都要重排。半截的 `**` 必须先退化成普通文本、
 *      下一帧补齐后再变回加粗；「先拼 HTML 字符串再整体替换」的写法很难做到，
 *      还会丢焦点。
 *   3. 不引新依赖。
 *
 * 覆盖范围刻意保守：只处理 RAG 答案里真会出现的语法，没覆盖的原样当纯文本显示，
 * 宁可少渲染也不要渲染错。
 */

// 行内语法。顺序有讲究：**粗体** 必须排在 *斜体* 前面试，
// 否则 `**30日**` 会被拆成两个斜体。
const INLINE_RE = /(\*\*[^*\n]+?\*\*|`[^`\n]+?`|~~[^~\n]+?~~|\*[^*\n]+?\*)/g

const CJK_RE = /[　-〿一-鿿＀-￯]/

/** 软换行拼接：中文之间不补空格（会显出多余空隙），西文之间补（否则单词被粘连）。 */
function joinSoft(a, b) {
  return CJK_RE.test(a.slice(-1)) && CJK_RE.test(b.slice(0, 1)) ? a + b : `${a} ${b}`
}

function foldLines(lines) {
  let text = ''
  for (const s of lines) text = text ? joinSoft(text, s) : s
  return text
}

function renderInline(text, key) {
  const parts = []
  let last = 0
  let m
  INLINE_RE.lastIndex = 0
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    const k = `${key}-${m.index}`
    if (tok.startsWith('**')) parts.push(<strong key={k}>{tok.slice(2, -2)}</strong>)
    else if (tok.startsWith('`')) parts.push(<code key={k}>{tok.slice(1, -1)}</code>)
    else if (tok.startsWith('~~')) parts.push(<del key={k}>{tok.slice(2, -2)}</del>)
    else parts.push(<em key={k}>{tok.slice(1, -1)}</em>)
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

const RE_OL = /^(\s*)(\d{1,3})[.)]\s+(.*)$/
const RE_UL = /^(\s*)[-*+]\s+(.*)$/
const RE_H = /^(#{1,6})\s+(.*)$/
const RE_QUOTE = /^\s*>\s?(.*)$/
const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/
const RE_FENCE = /^\s*```(.*)$/

const isBreak = (l) =>
  RE_OL.test(l) || RE_UL.test(l) || RE_H.test(l) || RE_QUOTE.test(l) ||
  RE_HR.test(l) || RE_FENCE.test(l)

function parseBlocks(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }

    // 围栏代码块
    const fence = RE_FENCE.exec(line)
    if (fence) {
      const lang = (fence[1] || '').trim()
      const buf = []
      i++
      while (i < lines.length && !RE_FENCE.test(lines[i])) { buf.push(lines[i]); i++ }
      i++ // 跳过收尾围栏（没闭合也照样收尾，避免把后面全吃进来）
      blocks.push({ t: 'code', lang, text: buf.join('\n') })
      continue
    }

    if (RE_HR.test(line)) { blocks.push({ t: 'hr' }); i++; continue }

    const h = RE_H.exec(line)
    if (h) { blocks.push({ t: 'h', level: h[1].length, text: h[2] }); i++; continue }

    if (RE_QUOTE.test(line)) {
      const buf = []
      while (i < lines.length) {
        const q = RE_QUOTE.exec(lines[i])
        if (!q) break
        buf.push(q[1].trim())
        i++
      }
      blocks.push({ t: 'quote', text: foldLines(buf) })
      continue
    }

    // 列表：按缩进分同级/子级，只支持一层嵌套（够用了，再深容易误判）
    if (RE_OL.test(line) || RE_UL.test(line)) {
      const baseIndent = (RE_OL.exec(line) || RE_UL.exec(line))[1].length
      const items = []
      let cur = null
      while (i < lines.length) {
        const L = lines[i]
        if (!L.trim()) {
          // 空行后若仍是同级列表项则继续，否则列表结束
          const nx = lines[i + 1] || ''
          const nOl = RE_OL.exec(nx)
          const nUl = RE_UL.exec(nx)
          const nIndent = Math.min(nOl ? nOl[1].length : Infinity,
                                   nUl ? nUl[1].length : Infinity)
          if (nIndent >= baseIndent) {
            if (cur) { items.push(cur); cur = null }
            i++
            continue
          }
          break
        }
        const mOl = RE_OL.exec(L)
        const mUl = RE_UL.exec(L)
        if (mOl || mUl) {
          const node = {
            text: (mOl ? mOl[3] : mUl[2]).trim(),
            ordered: !!mOl,
            children: [],
          }
          if ((mOl ? mOl[1].length : mUl[1].length) > baseIndent) {
            if (cur) cur.children.push(node)
            else items.push(node)
          } else {
            if (cur) items.push(cur)
            cur = node
          }
          i++
          continue
        }
        // 缩进续行并入上一项
        const cont = /^(\s{2,})(\S.*)$/.exec(L)
        if (cur && cont) { cur.text = joinSoft(cur.text, cont[2].trim()); i++; continue }
        break
      }
      if (cur) items.push(cur)
      blocks.push({ t: 'list', items })
      continue
    }

    // 段落：连续非空行合成一个
    const buf = []
    while (i < lines.length && lines[i].trim() && !isBreak(lines[i])) {
      buf.push(lines[i].trim())
      i++
    }
    if (!buf.length) { i++; continue }   // 防御：避免死循环
    blocks.push({ t: 'p', text: foldLines(buf) })
  }

  return blocks
}

function renderList(items, key, tail) {
  if (!items.length) return null
  const Tag = items[0].ordered ? 'ol' : 'ul'
  return (
    <Tag className="md-list" key={key}>
      {items.map((it, i) => (
        <li key={i} className="md-li">
          <span>
            {renderInline(it.text, `${key}-${i}`)}
            {i === items.length - 1 && it.children.length === 0 ? tail : null}
          </span>
          {renderList(it.children, `${key}-${i}-c`, null)}
        </li>
      ))}
    </Tag>
  )
}

/**
 * @param {string} text  markdown 原文
 * @param {ReactNode} tail  流式光标。塞进**最后一个块内部**而不是当兄弟节点追加，
 *   否则它会独占一行，光标会跟着每个新 token 上下跳一行。
 */
export default function Markdown({ text, tail = null }) {
  const blocks = parseBlocks(text)
  const last = blocks.length - 1
  return (
    <div className="md">
      {blocks.map((b, i) => {
        const k = `b${i}`
        const t = i === last ? tail : null
        switch (b.t) {
          case 'h': {
            // 模型爱用 # 开场，聊天正文里不该有超大标题，整体下压一级
            const Tag = `h${Math.min(b.level + 1, 6)}`
            return <Tag key={k} className="md-h">{renderInline(b.text, k)}{t}</Tag>
          }
          case 'quote':
            return (
              <blockquote key={k} className="md-quote">
                {renderInline(b.text, k)}{t}
              </blockquote>
            )
          case 'code':
            return <pre key={k} className="md-code"><code>{b.text}</code></pre>
          case 'list':
            return renderList(b.items, k, t)
          case 'hr':
            return <hr key={k} className="md-hr" />
          default:
            return <p key={k} className="md-p">{renderInline(b.text, k)}{t}</p>
        }
      })}
    </div>
  )
}