# AI Library Management Agent: Implementation Plan

> Based on [SPEC.md](SPEC.md). Each task names the spec layer it implements. Tick the boxes as work is done.

**Goal:** Replace the handwritten library register with an AI agent and database. The library committee can register borrowing and returns quickly, every book is tracked, and overdue, damage and fine cases are handled with a human in control.

**Approach:** A small web app over a library database. A Claude-powered agent reads and writes the database only through a fixed set of tools. Low-risk tools run on their own. Every higher-risk write becomes a **pending action** that a committee member must approve before anything changes. A daily scheduled job handles due-date reminders, overdue alerts and the school email.

**Proposed tech stack**

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Simple, well supported |
| Database | SQLite (one file, backed up to cloud storage) | No server needed for one school library |
| Web app | FastAPI with simple HTML pages | Covers the "website" interface in Layer 6 |
| AI agent | Claude API with tool use | Picks and calls the tools from Layer 6 |
| Voice entry | The browser's built-in speech-to-text, filling the text box | Supports dictated entries with no extra service |
| Notifications | School email over SMTP | Reminders, overdue alerts, school escalation and fine messages |
| Scheduler | One daily job (cron or Windows Task Scheduler) | Due-date reminders and overdue checks |
| Tests | pytest | Runs the 20 test cases |

---

## Phase 0: Settle open decisions (before coding)

The committee and facilitator need to answer these first, because they change how the tools are built.

- [ ] **Reminders:** Confirm that standard due-date reminders are automatic, and that custom or escalated ones need confirmation.
- [ ] **Fines:** Confirm that the agent drafts the pay-up message and sends it only after a human sets the amount.
- [ ] **Borrowing approval:** Decide whether every borrowing is approved or only exceptions (for example a student with unpaid fines, an overdue book, or a lost/damaged history). *Suggestion: approve every borrowing for the first term, then move to exceptions only if records stay accurate.*
- [ ] **Damage recording:** Confirm raising the risk rating from medium to high.
- [ ] **Policy numbers:** Get the loan limit per student, how many renewals are allowed, how long a reservation is held, after how many overdue days the school is emailed, and the school mail ID.
- [ ] **Festival due dates:** Agree on who may change a due date around festivals. *Suggestion: the librarian only, recorded in the correction log.*

**Done when:** every answer is written into SPEC.md under "Open decisions".

> **Status:** the first build uses the suggested answers as provisional defaults, listed in SPEC.md under "Provisional answers used in the first build". These boxes stay open until the committee confirms them.

---

## Phase 1: Database and register (Layers 2, 3, 4)

- [x] **1.1 Create the database tables**
  - `books`: book ID, name, author, copy number, status, condition notes
  - `students`: student ID, name, grade, contact email. *(Layer 4: nothing else, no passwords.)*
  - `loans`: book, student, take date, due date, return date, renewals, approved by
  - `reservations`: book, student, reserved on, expires on, active
  - `fines`: loan, reason, amount, set by, status (unpaid / paid / waived / reduced)
  - `pending_actions`: tool, details, suggested by, reason, status, decided by
  - `human_decisions`: special approvals, renewals, fines waived or reduced, lost confirmations, damage assessments
  - `correction_log`: original AI decision, human correction, reason, date. Entries can be added but never edited or deleted.
- [x] **1.2 Book states and allowed transitions.** Add the 7 states and only the transitions in the Layer 3 table. Reject any other change, except a librarian correction, which always writes a correction log entry.
- [x] **1.3 Due-date rule.** 7 days, Friday to Friday. A book taken on another day is due on the next Friday at least 7 days later *(confirm in Phase 0)*.
- [x] **1.4 Total and current books.** Add a count of all books and of books currently in the library.
- [x] **1.5 Import the existing register.** Load the current book catalogue and student list from a spreadsheet (CSV).

**Done when:** the database can be created from scratch, the catalogue is imported, an illegal transition such as `Available → Lost` is refused, and the totals are correct.

---

## Phase 2: Tools (Layer 6)

Every tool records who used it and when. Write tools never change records directly. They create a pending action.

**Read tools**

- [x] **2.1 Find book** (low risk): search by name, author or book ID
- [x] **2.2 Check availability** (low risk): status, who has it, and the due date
- [x] **2.3 Find student record** (medium risk): name, grade, current loans, unpaid fines. Show only what is needed for the task.

**Write tools (create a pending action and wait for approval)**

- [x] **2.4 Save borrowing details** (high): check the loan limit, unpaid fines and lost/damaged history, then propose the loan with its due date
- [x] **2.5 Update return status** (high): propose the return, and say whether the book came back in good condition
- [x] **2.6 Record book damage** (high, raised from medium): propose damage with notes. The book stays out of circulation until a human decides.

**Tools added from the open decisions**

- [x] **2.7 Reserve book**, and expire reservations automatically
- [x] **2.8 Renew loan:** the agent proposes, a human approves. It checks the reservation status and due date first (Layer 4).
- [x] **2.9 Mark lost:** human only
- [x] **2.10 Delete record:** human only, needs a reason. It is a soft delete, so the record is hidden but kept for the audit.

**Human-only actions (not available to the agent at all)**

- [x] **2.11** Set, waive or reduce a fine
- [x] **2.12** Change a due date (for example around festivals)
- [x] **2.13** Correct any book state (librarian override, written to the correction log)
- [x] **2.14** Approve or reject a pending action

**Done when:** each tool has a test, and the agent's tool list contains none of the human-only actions.

---

## Phase 3: Notifications and scheduled jobs (Layers 8, 9)

- [x] **3.1 Message templates:** due-date reminder, overdue reminder, red alert (for the committee), school email, pay-up message. Use neutral wording. Never blame a student (Layer 7).
- [x] **3.2 Daily job:**
  - send the reminder on the due date (automatic)
  - move books past their due date to `Overdue` and show a red alert
  - after the agreed number of days, email the school mail ID with the number of unreturned books
  - expire old reservations
- [x] **3.3 Pay-up message:** the agent drafts it after damage is recorded, and it can be sent only once a human has set the fine amount.
- [x] **3.4 No duplicates:** running the daily job twice on the same day must not send messages twice.

**Done when:** a simulated week of daily runs sends exactly the expected reminders, alerts and school email.

---

## Phase 4: The AI agent (Layers 5, 7)

- [x] **4.1 System prompt:** start from the Layer 7 prompt and add these rules:
  - never decide fines, deletions, due-date changes or who is responsible for a missing book
  - always say when an action is waiting for approval
  - never store passwords or unnecessary personal data
  - say "I don't know" rather than guess
- [x] **4.2 Agent loop:** the user's request goes to the agent, which calls tools and replies. It stops after a fixed number of steps.
- [x] **4.3 When the agent doesn't know (Layer 5):** search the register and rules, ask the user, check the database, suggest escalating to the librarian, then say "I don't know".
- [x] **4.4 Reasoning checks (Layer 7):** before proposing a loan, look at the student's history. Flag unpaid fines, overdue books, and lost or damaged reports for a human. Don't refuse automatically.
- [x] **4.5 Using memory (Layer 4):** show relevant past human decisions, such as a similar renewal approval, as guidance only. Always re-check the current state.
- [x] **4.6 Knowledge files:** store the library rules, borrowing limits, fine policy and repair guidelines as documents the agent can read. Keep the fine policy hidden from anyone who shouldn't see it.

**Done when:** the agent handles typed requests end to end in tests, using a fake AI client so the tests don't need an API key.

---

## Phase 5: Web interface and human in the loop (Layers 1, 9)

- [x] **5.1 Chat page:** a text box with a microphone button for dictated entries
- [x] **5.2 Approvals page:** lists pending actions. For each one it shows the student name and grade, book name and ID, borrow and due dates, status, and the agent's suggested action with its reason. Each has Approve and Reject buttons, and Reject needs a reason.
- [x] **5.3 Pause and resume:** the agent's task waits on the pending action, and continues once a human approves or rejects it.
- [x] **5.4 Register page:** total and current books, every book's status, red alerts for overdue books
- [x] **5.5 Librarian tools page:** set fines, mark lost, delete, change due dates, correct states. Each one asks for a reason.
- [x] **5.6 Correction log page:** a read-only list of all corrections
- [ ] **5.7 Committee login:** each committee member signs in so approvals record a real name. The library app does not store passwords itself. Use the school's existing accounts or a simple shared sign-in service.
  - *Built:* `AUTH_MODE=header` takes the signed-in email from a school sign-in proxy and checks it against `members.csv`. `AUTH_MODE=dev` (pick a name) is for local testing only. Still to do: connect the school's sign-in service.

**Done when:** a committee member can register a borrowing by voice, approve it, and see the register update.

---

## Phase 6: Testing (Layer 10)

The 5 written test cases plus 15 new ones. These 15 also complete the "15 more test cases" item in SPEC.md.

| # | Test case | Expected result | Type |
|---|---|---|---|
| 1 | Check book availability | Correct status shown | Written |
| 2 | Borrow a book | Correct details saved after approval | Written |
| 3 | Return a book | Status updated after approval | Written |
| 4 | Book overdue | Reminder sent and red alert shown | Written |
| 5 | Book damaged | Agent asks for a human decision | Written |
| 6 | Borrowing is rejected by the human | Nothing is saved, rejection is logged | New, edge |
| 7 | Student is over the loan limit | Agent flags it and asks for approval | New, edge |
| 8 | Student has an unpaid fine | Agent flags it before proposing the loan | New, edge |
| 9 | Book is reserved for a different student | Borrowing is refused | New, edge |
| 10 | Reservation expires | Book goes back to Available automatically | New, normal |
| 11 | Renewal approved | Due date moves one loan period | New, normal |
| 12 | Asked to change a due date | Agent refuses and refers it to the librarian | New, edge |
| 13 | Asked to set a fine | Agent refuses and asks a human | New, edge |
| 14 | Pay-up message before the amount is set | Not sent | New, edge |
| 15 | Book still not returned after N days | School is emailed with the number of unreturned books | New, normal |
| 16 | Book cannot be found | Agent doesn't blame the student and asks the librarian to decide | New, edge |
| 17 | Delete a record | Only by a human with a reason, and kept in the audit | New, edge |
| 18 | Librarian overrides the agent | Correction log has the original decision, correction, reason and date | New, normal |
| 19 | Question the agent can't answer | Agent says "I don't know" and does not make anything up | New, edge |
| 20 | User types a password into the chat | It is not stored anywhere | New, edge |

- [x] **6.1** Automate all 20 as pytest tests
- [ ] **6.2** Do a one-week trial alongside the paper register and compare the two
- [ ] **6.3** Measure success (Layer 10): spot-check accuracy against the shelf, count errors, and time each entry against the paper register

**Done when:** all 20 tests pass, and the trial week shows fewer errors and less time spent than the paper register.

---

## Phase 7: Launch and backup

- [ ] **7.1** Back up the database to cloud storage every day, and test restoring it once
  - *Built:* `backup` and `restore` commands, with an automated restore test. Still to do: schedule the backup daily into a cloud-synced folder, and test a restore on the school machine.
- [x] **7.2** A one-page guide for committee members: how to register, return, approve and handle alerts
- [ ] **7.3** Go live, keeping the paper register for the first two weeks as a fallback
- [ ] **7.4** Review the correction log monthly to see where the agent gets things wrong

---

## Still to do in the spec

- [x] Memory architecture drawing (Layer 4). Draw it during Phase 1, once the tables are final.
- [ ] Copy test cases 6–20 from Phase 6 into SPEC.md once the committee agrees with them.

## Suggested order and timing

| Week | Work |
|---|---|
| 1 | Phase 0 decisions, Phase 1 database |
| 2 | Phase 2 tools |
| 3 | Phase 3 notifications, Phase 4 agent |
| 4 | Phase 5 web interface |
| 5 | Phase 6 testing and trial week |
| 6 | Phase 7 launch |
