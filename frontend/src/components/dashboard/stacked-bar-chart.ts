import { LitElement, html, nothing } from 'lit'
import { property } from 'lit/decorators.js'
import type { StackedBar } from '../../utils/dashboard-data'
import { formatPounds } from '../../utils/dashboard-data'

import tokens from '../../styles/dashboard-tokens.css'
import styles from '../../styles/stacked-bar-chart.css'

/**
 * Pure-CSS stacked bar chart: the energy-used segment sits on top of the fixed
 * daily standing charge, so the fixed floor a customer pays before using
 * anything is always visible. Height-normalised to the max total in the series.
 */
class EonStackedBarChart extends LitElement {
  static styles = [tokens, styles]

  @property({ type: Array }) bars: StackedBar[] = []
  @property() usageColor = 'var(--eon-elec)'
  @property() standColor = 'var(--eon-elec-standing)'
  /** Off-peak usage colour, used only for bars that carry a time-of-use split. */
  @property() offpeakColor = 'var(--eon-green-light)'
  /** Chart body height in px. */
  @property({ type: Number }) height = 240
  /** Max bar width in px (keeps sparse series from ballooning). */
  @property({ type: Number }) maxBarWidth = 22
  /** Whether to render the x-axis label row. */
  @property({ type: Boolean }) showLabels = true

  render() {
    // The gap between columns is fixed px and cannot shrink, so dense ranges
    // (90d ~ 90 bars) would otherwise blow the row past the card width - the
    // columns collapse to zero and nothing renders. Scale the gap down as the
    // series grows so the whole row always fits its container.
    const gap = Math.max(
      1,
      Math.min(5, Math.floor(150 / Math.max(1, this.bars.length - 1)))
    )
    return html`
      <div
        class="bars"
        style="height:${this.height}px;gap:${gap}px"
        role="img"
        aria-label="Daily cost - energy used stacked on the standing charge"
      >
        ${this.bars.map((b) => this._renderBar(b))}
      </div>
      ${
        this.showLabels
          ? html`<div class="labels" style="gap:${gap}px">
              ${this.bars.map((b) => html`<span class="label">${b.label}</span>`)}
            </div>`
          : nothing
      }
    `
  }

  private _renderBar(bar: StackedBar) {
    if (bar.missing) {
      return html`
        <div
          class="col col--missing"
          style="max-width:${this.maxBarWidth}px"
          title="No usage data received for this day yet"
        >
          <div class="stack stack--missing"></div>
        </div>
      `
    }
    const split =
      bar.splitAvailable === true &&
      bar.peakPct !== undefined &&
      bar.offpeakPct !== undefined
    const title = split
      ? `${formatPounds(bar.usageCost + bar.standCost)} · peak ${formatPounds(
          bar.peakCost ?? 0
        )} + off-peak ${formatPounds(bar.offpeakCost ?? 0)} + standing ${formatPounds(
          bar.standCost
        )}`
      : `${formatPounds(bar.usageCost + bar.standCost)} · usage ${formatPounds(
          bar.usageCost
        )} + standing ${formatPounds(bar.standCost)}`
    // Stack order top→bottom: peak usage, off-peak usage, then the fixed
    // standing-charge floor. Non-split bars draw a single usage block.
    const usage = split
      ? html`
          <div
            class="seg seg--usage"
            style="height:${bar.peakPct}%;background:${this.usageColor}"
          ></div>
          <div
            class="seg seg--offpeak"
            style="height:${bar.offpeakPct}%;background:${this.offpeakColor}"
          ></div>
        `
      : html`
          <div
            class="seg seg--usage"
            style="height:${bar.usagePct}%;background:${this.usageColor}"
          ></div>
        `
    return html`
      <div class="col" style="max-width:${this.maxBarWidth}px" title=${title}>
        <div class="stack">
          ${usage}
          <div
            class="seg seg--stand"
            style="height:${bar.standPct}%;background:${this.standColor}"
          ></div>
        </div>
      </div>
    `
  }
}

if (!customElements.get('eon-stacked-bar-chart')) {
  customElements.define('eon-stacked-bar-chart', EonStackedBarChart)
}

export { EonStackedBarChart }
