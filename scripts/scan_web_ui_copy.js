// Precise UI-copy scan using the TypeScript parser (JSX-aware, no regex guessing).
const ts = require('typescript')
const fs = require('fs')
const path = require('path')

const ROOT = process.argv[2] || 'src'
const ATTRS = new Set(['placeholder', 'title', 'aria-label', 'alt', 'label'])
const LATIN = /[A-Za-z]{3,}/
const PERSIAN = /[\u0600-\u06ff]/
const COPY = /^[A-Za-z0-9][A-Za-z0-9 ,.'!?&/()%:;+\-–—…]*$/

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (['node_modules', 'dist', 'build'].includes(entry.name)) continue
    const p = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(p, out)
    else if (p.endsWith('.tsx')) out.push(p)
  }
  return out
}

const results = {}
for (const file of walk(ROOT)) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits = []
  const line = (node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1

  const visit = (node) => {
    if (ts.isJsxText(node)) {
      const t = node.text.replace(/\s+/g, ' ').trim()
      if (t && LATIN.test(t) && !PERSIAN.test(t)) hits.push({ line: line(node), kind: 'text', text: t })
    }
    if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer)) {
      const name = node.name.getText(sf)
      const value = node.initializer.text
      if (ATTRS.has(name) && LATIN.test(value) && !PERSIAN.test(value)) {
        hits.push({ line: line(node), kind: `attr:${name}`, text: value })
      }
    }
    if (ts.isJsxExpression(node) && node.expression) {
      const e = node.expression
      if (ts.isConditionalExpression(e) && ts.isStringLiteral(e.whenTrue) && ts.isStringLiteral(e.whenFalse)) {
        const a = e.whenTrue.text
        const b = e.whenFalse.text
        const english = [a, b].filter((s) => LATIN.test(s) && !PERSIAN.test(s) && COPY.test(s))
        if (english.length === 2 && (a.length > 3 && b.length > 3) && (a.includes(' ') || b.includes(' '))) {
          hits.push({ line: line(node), kind: 'ternary', text: JSON.stringify(a) + ' : ' + JSON.stringify(b) })
        }
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  if (hits.length) results[file] = hits
}

let total = 0
for (const [file, hits] of Object.entries(results)) {
  console.log(`\n=== ${file} (${hits.length})`)
  for (const h of hits.slice(0, 10)) console.log(`  ${String(h.line).padStart(5)} ${h.kind.padEnd(12)} ${h.text.slice(0, 90)}`)
  if (hits.length > 10) console.log(`  ... ${hits.length - 10} more`)
  total += hits.length
}
console.log('\ntotal candidates:', total, 'files:', Object.keys(results).length)
