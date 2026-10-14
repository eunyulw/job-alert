# Email-alert fixture: `linkedin-alert.txt`

**Constructed, not captured.** On 2026-10-14 the mailbox held no LinkedIn job-alert email to
capture, so this file reproduces the plain-text layout LinkedIn job-alert emails use (title,
company, location, an optional status line, and a `View job:` link per card, cards separated by a
rule) with invented job ids and titles. It pins the parser's rules, nothing more.

The Tesla email-alert source stays disabled until a real alert email has been parsed: the first
real message replaces this file (with personal data removed) and the parser tests are re-run
against it. The text names no person.
