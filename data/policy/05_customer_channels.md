# Customer contact channels: IVR, callback and chat (WFM-POL-05)

Applies to changes in how callers are greeted, queued or redirected. Owner: Customer
Experience. Version 2026-09.

## CX-01 IVR known-issue message
A known-issue message ("we are aware that UPI payments are failing...") may be played only
when a confirmed incident or a clear spike in one call reason supports it. The message must
be factual, must not promise a fix time, and must only target callers with the related
reasons. In practice such a message stops at most 25% of the related calls.

## CX-02 Callback offer
Callers may be offered a callback instead of waiting only while the predicted wait is
longer than 120 seconds. About 35% of callers accept. Every accepted callback must be
returned the same day.

## CX-03 Moving callers to chat or self-service
At most 20% of eligible calls may be moved to chat or self-service. Only these reasons are
eligible: payment status enquiry, refund status, balance enquiry and new offer enquiry.
Each chat contact costs Rs 12.

## CX-04 Fraud calls are never deflected
Callers reporting fraud must always reach an agent. Fraud reports may never be offered a
callback, an IVR message that ends the call, or a move to chat.

## CX-05 Customer-experience impact
IVR messages and callback offers have a mild customer-experience impact. Prefer staffing
levers when they are enough, and switch customer-facing changes off as soon as the spike
ends.
