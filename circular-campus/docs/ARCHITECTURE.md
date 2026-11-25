# Class and architecture plan

How the Phase 4-5 design maps to the Phase 6 code. Section and figure numbers
refer to the Phase 4-5 design document; requirement IDs refer to the Phase 3 SRS.

## 1. Architecture

A modular monolith (DR-1): one Django project, one database, one module per
Level 1 process. Pages are server-rendered Django templates (DR-2).

```
Browser (HTML, CSS)
   |
Views            <app>/views.py     read the request, call a service, pick a page
   |
Services         <app>/services.py  business rules, one transaction per action
   |
Domain classes   <app>/models.py    data, state machines, class-diagram operations
   |
Database         PostgreSQL (SQLite for local runs)
```

Rules for the layers:

- Views never change a record directly. They call a service.
- Every service method starts with a permission check and runs in one
  transaction, so stock, custody, history and notifications change together.
- Only `ai/gateway.py` talks to an AI provider (DR-3).

| Process | Module | Main classes |
| --- | --- | --- |
| 1.0 Access | `accounts` | `User`, `Department`, permission rules |
| 2.0 Catalog | `catalog` | `Asset`, `Category`, `AssetEvent`, `CatalogService` |
| 3.0 Requests and matching | `exchange`, `ai` | `ResourceRequest`, `Reservation`, `MatchingService` |
| 4.0 Approvals and transfers | `exchange` | `ApprovalCase`, `ApprovalDecision`, `Transfer`, `ExchangeService` |
| 5.0 Life cycle | `catalog` | state machine and history (inspection, repair and end of life come later) |
| 6.0 Reporting | `reports` | `ImpactService`, `ReportService`, `ReportDraft` |
| 7.0 AI orchestration | `ai` | `AIGateway`, `ClaudeAdapter`, `ClassicAdapter`, `AIRun`, `Recommendation`, `Advice`, and one service per feature |
| Notifications | `notifications` | `Notification`, `NotificationService` |

## 2. Class diagram to code

Domain classes (Figure 26):

| Design class | Code | Attributes | Operations |
| --- | --- | --- | --- |
| User | `accounts.models.User` | role, department, active | `can(action, record)` |
| ResourceRequest | `exchange.models.ResourceRequest` | quantity, min_condition, state, required_date | `submit` is `ExchangeService.submit`; `remaining_quantity()`; `move_to(state)` |
| Asset | `catalog.models.Asset` | category, condition, quantity, state, location, department, owner | `eligible_for(request)`, `transition(...)` |
| Reservation | `exchange.models.Reservation` | request, asset, quantity, expires_at, state | released by `ExchangeService._release` |
| ApprovalCase | `exchange.models.ApprovalCase` | reservation, stage, policy_version, state | `ready_for_execution()` |
| ApprovalDecision | `exchange.models.ApprovalDecision` | case, approver, result, reason, time | `validate_authority()` |
| Transfer | `exchange.models.Transfer` | reservation, state, dispatched_qty, received_qty | `ExchangeService.dispatch`, `ExchangeService.receive` |

Multiplicities are as designed: a request has 0..* reservations, an asset has
0..* reservations, a reservation has exactly one approval case and 0..1
transfer, and a case has 0..* decisions.

Service classes (Figure 27):

| Design class | Code | Operations built now |
| --- | --- | --- |
| ExchangeService | `exchange.services.ExchangeService` | `submit`, `reserve`, `decide` (approve or reject), `dispatch`, `receive`, `cancel` |
| AIGateway | `ai.gateway.AIGateway` | `classify`, `rank`, `advise`, `converse`, `summarise_report` |
| AIRun | `ai.models.AIRun` | task, model id, config version, input snapshot, output, fallback reason |
| ImpactService | `reports.services.ImpactService` | `calculate(start, end)` |
| LifeCycleService | not built yet | inspection, repair and terminal proposals |
| ImpactRecord | not stored yet | figures are calculated on request from completed transfers |

Added in code, not in the diagram: `Department`, `Category`, `AssetEvent`
(the history), `Recommendation`, `Advice`, `ReportDraft`, `Notification`,
`CatalogService`, `ClassificationService`, `MatchingService`, `AdviceService`,
`AssistantService`, `ReportService`, `NotificationService`.
Add these to the class diagram for the final report.

## 3. State machines

Asset (Figure 33), enforced by `Asset.ALLOWED_TRANSITIONS`:

```
Recorded -> Available -> Held -> In transit -> In use
               ^           |                     |
               +-----------+ reject/cancel       |
               +---------------------------------+ declare surplus
Available or In use -> Blocked unsafe -> Available or In use
Available, In use or Blocked -> Terminal
Available -> Recorded (withdraw listing, UC-16)
```

Request (Figure 34): Submitted -> Under review -> Approved -> Part fulfilled
-> Fulfilled; Under review -> Rejected; any open state -> Cancelled.

Transfer (Figure 35): Ready -> In transit -> Completed; Ready -> Cancelled.

Approval (Figure 36): Under review -> Approved, Rejected or Withdrawn.

Any other move raises `IllegalTransition`. Each asset move writes one
`AssetEvent` (who, what, when, from, to), which cannot be edited or deleted.

## 4. How the hard rules are enforced

| Rule | How |
| --- | --- |
| Server checks permissions (C1) | `require(user, action, record)` in every view and service; unknown actions are denied |
| Custodian manages own department only (BR-10) | `asset.manage` compares departments; the department is set from the user, never from the form |
| No self-approval (BR-3) | `approval.decide` refuses the requester; `ApprovalDecision.validate_authority()` |
| Admin has no approval right (BR-16) | only the Approver role passes `approval.decide` |
| Last item cannot go to two people (BR-1) | the asset row is locked, then moved with an update that only succeeds if the state and quantity are unchanged |
| Owner and department change only on receipt, once (BR-6) | `ExchangeService.receive`; a second click finds the transfer Completed and changes nothing |
| Rejection or cancellation releases the hold once (BR-7) | `ExchangeService._release` |
| Cost visible to authorized roles only (BR-11) | `asset.view_cost` |
| Figures only with supporting data (BR-14) | `ImpactService` returns "not available" and counts the items left out |
| AI only recommends (BR-13) | recommendations do nothing until the requester clicks Accept; a summary is a draft until an admin publishes it |

Groups: when part of a group is reserved, those units are split into their own
asset record (linked by `parent`), so every record has exactly one state.

## 5. AI features

Classification (feature 1): at registration the gateway receives the name,
description and category names, and returns a category, condition, tags and
confidence. They are filled into the form; the custodian edits or accepts, and
the history notes which.

Sustainable-action advice (feature 3): the gateway receives the item's text,
category, condition, quantity and open demand, and returns one action with
reasons. Impact figures beside it come from `AdviceService.impact_if_kept_in_use`.
Accepting records the decision in the history; it does not change the state.

Assistant (feature 4): `AssistantService` fetches the available items that
match the question and the asker's own requests, and gives only that to the
gateway. Answers list real items; with no data it says "I don't know". It has
no way to change anything; item buttons lead to the normal request form.

Matching and ranking (feature 2, section 14):

1. Hard rules first: available, enough condition, right category, another
   department, not already rejected for this request.
2. The gateway sends only the need text and each item's name, description,
   category and condition. It returns a purpose-fit value from 0 to 1 and a
   one-line reason for each item.
3. Score = 100 x sum(weight x factor) / sum(weights with evidence), using the
   section 14 weights. Factors used now: purpose 0.25, quantity coverage 0.10,
   condition 0.10, location 0.10. The other four have no data yet, so the
   page shows evidence coverage 0.55.
4. The top 5 are stored as `Recommendation` rows; the requester accepts or
   rejects each one.

Generated report (feature 5): `ImpactService` calculates the figures,
`ReportService` sends only those numbers to the gateway, the narrative is
saved as a draft with the original AI wording, numbers in the text that are
not in the figures are flagged, and an administrator edits and publishes.

If the provider fails or takes longer than 15 seconds (CM-4), the
`ClassicAdapter` answers with keyword rules or a template, and the `AIRun`
records why.

## 6. Differences from the design to confirm

| Topic | Design | Phase 6 code |
| --- | --- | --- |
| Roles | nine roles with role assignments | four roles, one per user |
| In use -> Available | not drawn in Figure 33 | added as "declare surplus" so a received item can be exchanged again |
| Purpose factor | embedding cosine similarity | the model judges fit directly; the provider has no embedding endpoint |
| Notifications | outbox table and worker (DR-4) | in-app rows written in the same transaction; no email |
| Partial receipt, disputes | Figure 35 | not built; a transfer is received in full |
| Reservation expiry | RSV-3 | `expires_at` is stored; no job releases expired holds yet |
| Cost avoided | needs Procurement validation | labelled as an estimate from the recorded replacement cost |
| Carbon factors | per category | only the desktop factors quoted in SRS 6.2 are loaded |

## 7. Next steps

1. `LifeCycleService`: inspection, repair, safety block, end-of-life proposals.
2. Reservation expiry job, partial receipt and disputes.
3. Role assignments for the remaining five roles.
