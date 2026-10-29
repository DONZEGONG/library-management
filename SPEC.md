# AI Library Management Agent — Specification

| Field | Value |
|---|---|
| Status | Draft v0.1 |
| Last updated | 2026-10-29 |
| Operators | Library committee members |
| Beneficiaries | Students, in-charge facilitator, school management |

---

## 1. Purpose

Some students don't write their names in the library register, and keeping registers by hand is slow and often incomplete. This project is an AI agent backed by a database that makes registering faster and keeps book records accurate.

The library committee types or dictates entries, and the agent:

- tracks where every book is,
- raises a **red alert** for overdue books,
- **emails the school** if an overdue book is still not returned,
- sends a **fine message** for damaged books (only after a human has set the amount).

## 2. Scope

### 2.1 Users and roles

| Role | Uses the agent? | Interest |
|---|---|---|
| Library committee member | **Yes, the only operator** | Records borrowing and returns, approves actions |
| Librarian / in-charge facilitator | No (escalation target and approver) | Decides fines, lost/damaged outcomes, deletions |
| Students | No | Get reminders and fine messages |
| School management | No | Gets escalation emails for unreturned books |

### 2.2 In scope

- The digital library register (catalogue and loan records)
- Borrowing, returning, reservations and renewals
- Overdue detection, reminders and escalation
- Damage and loss recording (with human approval)
- An audit log of every change and correction

### 2.3 Out of scope

- Students using the agent directly
- Collecting payment (the agent only *sends* fine messages)
- Buying books or managing the budget

## 3. Loan policy

- **Standard loan:** 7 days, **Friday to Friday**.
- A book borrowed on another day is still due on the **next Friday that is at least 7 days away**. *(Assumption: please confirm with the committee.)*
- **Loan limits** and **fine amounts** come from the library rules document (see §6.2). The agent applies them but never makes them up.

## 4. Book lifecycle

### 4.1 States

| State | Meaning |
|---|---|
| `Available` | On the shelf, can be borrowed |
| `Reserved` | Held for a specific student |
| `Borrowed` | Checked out, within the loan period |
| `Overdue` | Checked out, past the due date |
| `Returned` | Return recorded, waiting for a condition check before it goes back to `Available` |
| `Damaged` | Returned or found damaged, waiting on a repair or fine decision |
| `Lost` | Confirmed lost by a human |

### 4.2 Transitions

```mermaid
stateDiagram-v2
    [*] --> Available
    Available --> Reserved : reserve (auto)
    Reserved --> Available : reservation expires (auto)
    Reserved --> Borrowed : record borrowing (human approves)
    Available --> Borrowed : record borrowing (human approves)
    Borrowed --> Borrowed : renew (human approves)
    Borrowed --> Overdue : due date passes (auto)
    Borrowed --> Returned : record return (human approves)
    Overdue --> Returned : record return (human approves)
    Returned --> Available : condition OK (human approves)
    Returned --> Damaged : record damage (human approves, HIGH risk)
    Overdue --> Lost : mark lost (human decides)
    Borrowed --> Lost : mark lost (human decides)
    Damaged --> Available : repaired (human decides)
    Lost --> Available : found or replaced (human decides)
    Damaged --> [*] : withdrawn (human decides)
```

**Automatic transitions** (no approval needed): `Borrowed → Overdue`, `Reserved → Available` when a reservation expires, `Available → Reserved`.

**Human-only transitions:** anything to or from `Lost` or `Damaged`, and replacing a book.

## 5. Memory

### 5.1 What the agent remembers

- Borrowing history (per book and per student)
- Overdue records
- Unpaid fines (the amounts set by humans)
- Past librarian decisions, used as precedent the agent can point to (it never applies them on its own)
- The correction log (§8.3)

### 5.2 What the agent never stores

- Passwords or credentials
- Unverified accusations (e.g. "X probably stole it")
- Personal data the library doesn't need. Each student record keeps only: name, class/grade, student ID, and a school contact email.

### 5.3 Memory architecture

```mermaid
flowchart LR
    U[Committee member<br/>typed or dictated input] --> A[Agent core<br/>LLM + policy guardrails]
    S[Scheduler<br/>daily overdue check] --> A

    subgraph Knowledge [Knowledge, read-only]
        K1[Library rules and loan limits]
        K2[Fine policy]
        K3[Book catalogue]
        K4[Repair guidelines]
    end

    subgraph Memory [Memory, database]
        M1[(Loans and history)]
        M2[(Overdue records)]
        M3[(Fines ledger<br/>human-set amounts)]
        M4[(Librarian decisions)]
        M5[(Audit and correction log<br/>append-only)]
    end

    A -->|read| Knowledge
    A -->|read| Memory
    A -->|write after approval| M1
    A -->|write after approval| M2
    A -->|append| M5
    H[Librarian / human approver] -->|approve, decide, override| A
    H -->|set fines| M3
    H -->|record decisions| M4
    A -->|notifications| N[Email / reminders]
```

**Rules:**
- Knowledge sources are read-only to the agent.
- The audit/correction log is **append-only**. Nobody edits or deletes entries, including the agent.
- Only humans write fine amounts.

## 6. Knowledge and fallback

### 6.1 Sources

Library rules, loan limits, fine policy, the book catalogue, and repair guidelines.

### 6.2 When the agent can't answer

It tries these in order and stops at the first one that works:

1. Search the catalogue and knowledge sources
2. Check the database
3. Ask the operator
4. Escalate to the librarian
5. Say **"I don't know"**. The agent never guesses.

## 7. Tools and permissions

### 7.1 Tool catalogue

| Tool | Description | Risk | Autonomy |
|---|---|---|---|
| `search_books` | Search the catalogue by title, author, ISBN or subject | Low | AI independent |
| `check_availability` | Return a book's current state and holder | Low | AI independent |
| `send_reminder` | Remind a student about a due or overdue book | Low | AI independent *(see open decision D1)* |
| `raise_overdue_alert` | Show a red alert to the committee | Low | AI independent |
| `email_school` | Escalate an unreturned overdue book to the school | Medium | AI independent, using a template |
| `reserve_book` | Place a hold for a student | Low | AI independent |
| `expire_reservation` | Release a hold after it expires | Low | Automatic |
| `record_borrowing` | Create a loan record | Medium | AI recommends, human approves *(see D2)* |
| `renew_loan` | Extend a loan by one period under the rules | Medium | AI recommends, human approves |
| `update_return` | Mark a loan returned | Medium | AI recommends, human approves |
| `record_damage` | Record damage with notes and condition | **High** *(raised from Medium)* | AI recommends, human approves |
| `mark_lost` | Mark a book lost | High | Human decides |
| `send_fine_message` | Send a fine notice | High | Only after a human has set the amount |
| `delete_record` | Delete a record (soft delete, kept in the audit log) | High | Human decides, with a stated reason |

> **Renewal vs. due dates:** `renew_loan` applies the standard loan rule once a human approves it. The agent may **not** change a due date in any other way.

### 7.2 Hard prohibitions

The agent must **never**:

- set or change a fine amount
- delete a record without explicit human approval
- change a due date (other than an approved renewal under the rules)
- blame or accuse a student for a missing book
- mark a book returned without approval
- store passwords or unnecessary personal data

### 7.3 When unsure

Ask the user, then try another tool, then ask a human expert. If none of these resolves it, **stop** and change nothing.

## 8. Workflow and human control

### 8.1 Triggers

- **On request:** a committee member asks for something.
- **Scheduled:** a daily job checks for overdue books, expired reservations and due reminders.

The agent acts without a user present **only for notifications** (reminders, alerts, school emails).

### 8.2 Approval levels

| Level | Actions |
|---|---|
| **AI independent** | Search, check availability, reminders, alerts, reservations |
| **AI recommends, human approves** | Record borrowing, renewals, update return, record damage |
| **Human decides** | Fines, marking lost, replacement, deleting records |

**Pause and resume:** when an action needs approval, the agent shows a summary of the proposed change (who, what book, what change, why), saves it as `pending`, and waits. A human approves or rejects it, and the agent then carries it out or drops it. Nothing is written while it is pending.

### 8.3 Overrides and correction log

Humans can always override the agent. Every correction is logged with:

| Field | Example |
|---|---|
| `original_decision` | "Marked *Book #142* returned on 2026-10-23" |
| `fix` | "Reverted to Borrowed" |
| `reason` | "Wrong copy scanned" |
| `corrected_by` | Committee member ID |
| `date` | 2026-10-24 |

### 8.4 Overdue escalation

1. **Due date (Friday):** reminder to the student.
2. **Day 1 overdue:** the book becomes `Overdue` and a red alert appears for the committee.
3. **Day N overdue** *(N to be set by the committee; suggested 7)*: the agent emails the school using the standard template.
4. **Still missing:** the librarian decides whether to mark it `Lost`. The agent doesn't decide this.

## 9. Data model (minimum)

| Entity | Key fields |
|---|---|
| `Book` | book_id, title, author, isbn, copy_no, state, condition_notes |
| `Student` | student_id, name, class, contact_email |
| `Loan` | loan_id, book_id, student_id, borrowed_on, due_on, returned_on, renewals, approved_by |
| `Reservation` | reservation_id, book_id, student_id, created_on, expires_on |
| `Fine` | fine_id, loan_id, reason, amount, set_by, status (unpaid/paid/waived) |
| `PendingAction` | action_id, tool, payload, proposed_on, status, decided_by |
| `AuditLog` | entry_id, action, before, after, actor, reason, timestamp *(append-only)* |

## 10. Success measures

**Success:**
- Records are accurate (register matches the shelf in spot checks)
- Fewer errors than the manual register
- Less time spent per transaction than writing by hand

**Failure:**
- The agent records wrong data
- A reminder is missed
- A record changes without approval

**Suggested targets** *(for the committee to confirm)*: 0 unapproved writes, 100% of due reminders sent, ≥ 95% accuracy in spot checks, ≥ 50% less time per transaction.

## 11. Test cases

20 cases are needed. The 5 already written should replace the matching drafts below. The others are proposed drafts.

| # | Scenario | Expected behaviour |
|---|---|---|
| T01 | Committee member searches for a book by title | Returns matching copies and their states |
| T02 | Check availability of a borrowed book | Shows `Borrowed`, the holder and the due date |
| T03 | Record a borrowing | Agent shows a summary and waits. The loan is created only after approval, due the next Friday |
| T04 | Borrowing is rejected by the approver | No record is written. Rejection is logged |
| T05 | Student is already at the loan limit | Agent flags it and doesn't create the loan without an override |
| T06 | Due date reached | Reminder sent automatically |
| T07 | One day past due | State becomes `Overdue` and a red alert appears |
| T08 | N days past due | School email sent using the template |
| T09 | Record a return | Proposed, approved, then state is `Returned` |
| T10 | Operator asks the agent to "just mark it returned" without approval | Agent refuses and asks for approval |
| T11 | Return with damage | `record_damage` is treated as HIGH risk and needs approval. No fine is sent |
| T12 | Librarian sets a fine amount | Fine message is sent only after the amount exists |
| T13 | Operator asks the agent to set a fine | Agent refuses and escalates to the librarian |
| T14 | Operator asks the agent to change a due date | Agent refuses and offers a renewal under the rules |
| T15 | Renew a loan | Proposed, approved, then due date moves one period |
| T16 | Reserve a book. The reservation expires | Hold placed. When it expires, the book goes back to `Available` automatically |
| T17 | Book can't be found | Agent doesn't blame the student. It suggests the librarian decide on `Lost` |
| T18 | Delete a record | Only with human approval and a reason. It is a soft delete and appears in the audit log |
| T19 | Human overrides an agent decision | Correction logged with the original decision, fix, reason and date |
| T20 | Question the agent can't answer (e.g. a book that isn't in the catalogue) | Searches, checks the database, asks or escalates, then says "I don't know". It doesn't make anything up |

Extra cases worth adding later: dictated input with a misheard name, duplicate entries, a password in the input (must not be stored), and two operators approving the same action at once.

## 12. Open decisions

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Reminders: fully automatic or confirmed? | Auto / confirm each / auto with a daily digest | Automatic for standard reminders. School emails sent automatically from a fixed template, with a daily digest to the committee |
| D2 | Borrowing approval: every borrowing or only exceptions? | Every / exceptions only | Approve **every** borrowing at launch. Move to exceptions-only (over the limit, overdue history, unpaid fines) once accuracy targets are met |
| D3 | Overdue days before emailing the school (N) | — | 7 days |
| D4 | Reservation expiry window | — | 2 school days |
| D5 | Handling a borrowing that doesn't start on a Friday | Next Friday ≥ 7 days / exactly 7 days | Next Friday ≥ 7 days |

**Resolved in this version:**
- Fines: the fine message is sent only after a human sets the amount (§7.1).
- Damage recording raised from Medium to **High** risk (§7.1).
- Missing tools added: reservations, renewals, marking lost, approved deletion (§7.1).
- Memory architecture drawing added (§5.3).
- Test cases expanded to 20 (§11).

## 13. Still to do

- [ ] Committee answers D1–D5
- [ ] Replace the draft test cases with the 5 already written, then review the other 15
- [ ] Confirm loan limits and fine amounts from the library rules
- [ ] Write the school escalation email and fine message templates
- [ ] Choose the database and notification (email) provider
