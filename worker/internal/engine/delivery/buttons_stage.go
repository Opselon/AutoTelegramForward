package delivery

import (
	"atf/worker/internal/domain/cleaning"
	"atf/worker/internal/domain/job"
)

// buttons_stage.go — F-16: the link policy must reach the source message's
// inline keyboard, not just its body text.
//
// Pyrogram's `message.copy` re-sends the source's `reply_markup` by default,
// so a task configured to remove links stripped the URLs from the text while
// every URL button under the message still reached the target. Python has
// `LinkManagementEngine.filter_inline_keyboard` (services.py:1065) for exactly
// this — but nothing on the Python side ever called it (the audit's F-16).
//
// The Go Worker is the business-logic owner and this is where the decision
// lands. The transform stage evaluates `cleaning.FilterInlineKeyboard` over
// the source's button snapshot and records the outcome on the payload
// (`drop_source_buttons`); the delivery side here only READS that flag and
// hands it to the Telegram runtime. One evaluation, one authority: the
// delivery engine never re-derives the policy, so a restart makes the same
// decision the ingest path made.
//
// The runtime contract keeps the flag additive (`false` = keep the source
// keyboard, the pre-4.4 behavior), so an older runtime that does not know
// the field simply ignores it and behaves as before.

// shouldDropSourceButtons reports whether the copy delivering this job must
// drop the source message's inline keyboard. It is the transform stage's
// recorded decision — see worker/internal/domain/cleaning/buttons.go for the
// evaluation itself and the pipeline wiring that sets it.
func shouldDropSourceButtons(j *job.DeliveryJob) bool {
	if j == nil {
		return false
	}
	return j.Payload.DropSourceButtons()
}

// buttonDecisionFor evaluates the link policy against the source message's
// inline-keyboard snapshot. The pipeline's body pass and this pass share one
// policy and one rule, so the text and the buttons get the same treatment —
// the F-16 invariant.
func buttonDecisionFor(j *job.DeliveryJob, cfg cleaning.Config) cleaning.ButtonDecision {
	rows := sourceButtonRows(j)
	if len(rows) == 0 {
		return cleaning.ButtonDecision{}
	}
	if cfg.LinkPolicy == "" {
		return cleaning.ButtonDecision{}
	}
	return cleaning.FilterInlineKeyboard(rows, cfg.LinkPolicy, cfg.DomainAllowlist, cfg.DomainBlocklist)
}

// sourceButtonRows decodes the payload's keyboard snapshot into the rows the
// filter evaluates. Malformed entries degrade to "no button" (a bad snapshot
// must never crash a delivery), and a row with no URL buttons is simply empty.
func sourceButtonRows(j *job.DeliveryJob) []cleaning.ButtonRow {
	raw := j.Payload.SourceButtons()
	if len(raw) == 0 {
		return nil
	}
	rows := make([]cleaning.ButtonRow, 0, len(raw))
	for _, item := range raw {
		rowList, ok := item.([]any)
		if !ok {
			continue
		}
		buttons := make([]cleaning.Button, 0, len(rowList))
		for _, entry := range rowList {
			btn, ok := entry.(map[string]any)
			if !ok {
				continue
			}
			url, _ := btn["url"].(string)
			text, _ := btn["text"].(string)
			buttons = append(buttons, cleaning.Button{Text: text, URL: url})
		}
		if len(buttons) > 0 {
			rows = append(rows, cleaning.ButtonRow{Buttons: buttons})
		}
	}
	return rows
}
