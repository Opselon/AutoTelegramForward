/**
 * The rule card's channel-flow diagram, one markup for every width.
 *
 * The card used to render two copies of the same diagram — `.channel-flow-mobile`
 * (vertical stack) and `.channel-flow-desktop` (horizontal row) — hidden and
 * shown by media queries. Any change had to be made twice, and the two copies
 * had already drifted (different badges, different labels). This component
 * renders ONE sequence of nodes and edges; CSS reflows it:
 *
 *   ≥ 768px  horizontal row:  A ─(mode)→ [C] ─→ B
 *   <  768px vertical stack:  the same nodes, arrows rotated
 *
 * Pure props (`rule`, `t`) and no internal state, so the memoized RuleCard can
 * render it without cost.
 */
import { memo } from 'react'
import { ArrowDown, ArrowRight, Sparkles } from 'lucide-react'
import { extraSourceCount } from './ShellWidgets'
import type { ForwardRule } from '../types'

type Translate = (key: string, params?: Record<string, string | number>) => string

/** First grapheme of a chat title for the avatar tile; ids fall back to a dot. */
function initialOf(name: string | undefined, id: string | undefined): string {
  const source = (name || id || '').trim()
  const chars = Array.from(source)
  return chars.length > 0 ? chars[0].toUpperCase() : '·'
}

function FlowDiagramImpl({ rule, t }: { rule: ForwardRule; t: Translate }) {
  const hop = Boolean(rule.use_intermediate)
  const sourceName = rule.source_chat_name || rule.source_chat_id
  const targetName = rule.target_chat_name || rule.target_chat_id
  const hopName = rule.intermediate_channel_name || rule.intermediate_channel_id || t('ui_channel_c')
  // Forward mode on the first edge: native pass-through vs. header copy.
  const modeLabel =
    rule.forward_mode === 'DIRECT_FORWARD' ? t('ui_native') : t('ui_flow_mode_copy')

  const sources = extraSourceCount(rule)

  const renderNode = (
    key: string,
    label: string,
    name: string,
    id: string | undefined,
    tone: '' | 'flow-node--hop' | 'flow-node--target',
    badge?: string,
  ) => (
    <div key={key} className={`flow-node ${tone}`.trim()}>
      <span className="flow-node__avatar" aria-hidden="true">
        {initialOf(name, id)}
      </span>
      <span className="flow-node__meta">
        <span className="flow-node__label">
          {label}
          {badge ? <span className="badge badge--warning flow-node__brand">{badge}</span> : null}
        </span>
        <span className="flow-node__name">{name}</span>
        {id ? <span className="flow-node__id font-mono-ltr">{id}</span> : null}
      </span>
    </div>
  )

  return (
    <div className="flow-diagram">
      {renderNode('a', t('ui_source_a'), sourceName, rule.source_chat_id, '', undefined)}
      {sources > 0 && (
        <span className="flow-node__extra badge badge--info">+{sources}</span>
      )}

      {/* Edge A → C (hop) or A → B (direct): the line carries the mode badge
          and the arrow, which CSS rotates for the vertical reflow and flips
          for RTL. */}
      <div className="flow-edge">
        <span className="flow-edge__line" aria-hidden="true" />
        <span className="flow-edge__chip">
          <span className="badge badge--neutral flow-edge__mode">{modeLabel}</span>
          <span className="flow-edge__arrow" aria-hidden="true">
            <ArrowRight className="flow-edge__arrow-h" size={14} />
            <ArrowDown className="flow-edge__arrow-v" size={14} />
          </span>
        </span>
        <span className="flow-edge__line" aria-hidden="true" />
      </div>

      {hop && (
        <>
          {renderNode(
            'c',
            t('ui_hop_c'),
            hopName,
            rule.intermediate_channel_id,
            'flow-node--hop',
            t('ui_brand_c'),
          )}
          <div className="flow-edge">
            <span className="flow-edge__line" aria-hidden="true" />
            <span className="flow-edge__chip">
              <span className="badge badge--warning flow-edge__mode">
                <Sparkles size={10} aria-hidden="true" />
                {t('ui_hop')}
              </span>
              <span className="flow-edge__arrow" aria-hidden="true">
                <ArrowRight className="flow-edge__arrow-h" size={14} />
                <ArrowDown className="flow-edge__arrow-v" size={14} />
              </span>
            </span>
            <span className="flow-edge__line" aria-hidden="true" />
          </div>
        </>
      )}

      {renderNode('b', t('ui_target_b'), targetName, rule.target_chat_id, 'flow-node--target')}
    </div>
  )
}

export const FlowDiagram = memo(FlowDiagramImpl)
