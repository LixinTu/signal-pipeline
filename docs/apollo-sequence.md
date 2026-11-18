# A small email sequence in Apollo

A step-by-step guide to running one outbound sequence in [Apollo](https://www.apollo.io) on its free plan. A sequence is a series of emails that go out on a schedule and stop when the person replies. It is used here for the owner's own job search (project 2), in the owner's own name.

Written on 2026-11-17. The script below ran once (2026-11-18 UTC) and created the inactive draft sequence; **the test and every send are not done, and none is planned**: the owner keeps the sequence as a draft. Menu names come from Apollo's 2022 blog post on sequences and from its help-center articles as quoted in search results; the help-center pages refused automated reading, so check every name against the screen. If a screen differs from a step below, stop and send a screenshot.

## Ground rules

- **The owner sends, nobody else.** This repo and the AI agent never send email. The owner starts every batch in Apollo after reading every email in it.
- **Only in the owner's own name.** The sample emails in the Brix work sample are written in Brix's voice and are never sent.
- **Test on your own inboxes first.** No real recipient gets an email until the test below has passed.
- **Small and slow.** At most 5 new recipients a day and 30 in total. This is practice with a real tool, not a campaign.
- **A mailbox you may use this way.** A personal Gmail account. Not a school or employer mailbox: their rules can forbid automated sending.
- **Every email has your real name and one line that lets the person say no**, such as "If this isn't relevant, tell me and I won't write again." Anyone who says no is removed the same day.
- **Stop rule.** Pause the sequence at the second bounce or the first complaint, and report it.
- **No purchases, no LinkedIn scraping.** Contacts come from Apollo's own search or from what the person has published. Names and emails stay in Apollo and HubSpot and never enter this repo.

## Limits to read in the account first

| Item | What the sources say | Source |
|---|---|---|
| Mailboxes on the free plan | Gmail accounts only | Apollo's pricing page |
| Default sending limits per mailbox | 50 a day, 6 an hour, 600 seconds between emails; Apollo advises staying at or below 50 a day | Apollo help center, as quoted in search results |
| Active sequences on the free plan | 2 | Third-party pages only |
| Daily sends on the free plan | 250 | Third-party pages only |

Write down what your account shows for each line. The plan above stays far below all of them.

## Creating the sequence with the script (ran once, 2026-11-18 UTC)

`apollo_sequence.py` creates the sequence through Apollo's API from the three draft emails in `automation/apollo/project2_sequence.json`, so steps 3 to 5 of the by-hand setup below are not needed. It creates the sequence inactive, with every email waiting for review, and it cannot add contacts, approve an email or activate anything.

1. In Apollo, open **Admin Settings > Integrations > API Keys** (the API Developer Portal) and click **Create new key**. Turn **Set as master key** off and select two endpoints only, `api/v1/emailer_schedules/index` and `api/v1/sequences/create`, so the key itself cannot add contacts or send. Put the key in `.env` as `APOLLO_API_KEY`. Never paste it into a chat. The free plan was enough for both endpoints.
2. `python apollo_sequence.py --dry-run` prints what would be sent and sends nothing.
3. `python apollo_sequence.py` creates the draft. A second run stops instead of creating a duplicate.
4. If Apollo answers with HTTP 403, the key's scope lacks one of the two endpoints, or the plan no longer allows them: build the sequence by hand as below.

Result of the first run: the sequence `Project 2: outreach draft (created by apollo_sequence.py)`, inactive, on the schedule `Normal Business Hours`, with a manual first email and two automatic follow-ups after 3 and 4 days, all three waiting for review. Six bracketed parts are still to fill in by hand.

The mailbox link, the sending limits, the test and every send are still done by the owner, in Apollo.

## Setup by hand

1. **Link the mailbox.** In Settings, open **Mailboxes** (one article gives the path Settings > Outbound > Mailboxes, another Settings > Email setup and health) and link the Gmail account with Google's sign-in. One mailbox can be linked to one Apollo account only.
2. **Lower the sending limits** for that mailbox: 5 a day and 2 an hour.
3. **Create the sequence.** **Engage > Sequences > + New Sequence**, start from scratch, name it `Project 2: test`, pick a weekday daytime schedule in your recipients' time zone, and click **+ Create**.
4. **Add three steps** with **+ Add Step**. Each step has a **Wait For** setting.

   | Step | Type | Wait | Thread |
   |---|---|---|---|
   | 1 | Manual Email for real recipients; Automatic Email for the test | None | New |
   | 2 | Automatic Email | 3 days (the smallest value offered, for the test) | Reply to step 1 |
   | 3 | Automatic Email | 4 days (the smallest value offered, for the test) | Reply to step 1 |

   A Manual Email creates a task with the template, and you edit and send each one yourself. That keeps the first email personal and checked; only the follow-ups are automatic.
5. **Write the emails** from the drafts below. Insert first name and company with the **{ }** button, not by typing braces. Apollo's preview marks a variable in red when a contact has no value for it.
6. **Save each email as a template**, then on the templates page use **Send test email to me** and read all three in your inbox.

## The test, with your own inboxes only

1. Add two contacts that are both you: two addresses you own, with a first name and a company filled in.
2. Add both to the sequence and activate it.
3. When step 1 arrives, check the name and company, the signature and the line that lets the person say no.
4. **Reply from the first inbox. Do nothing in the second.**
5. After the wait, the second inbox should get step 2 as a reply in the same thread, and the first inbox should get nothing more. If the first inbox also gets step 2, stop: the sequence does not stop on reply as expected.
6. Let step 3 arrive in the second inbox, then archive or pause the sequence.

Report: what arrived where and when, screenshots of the three steps and of the sequence's result numbers with addresses cropped, and the real limits from the table above.

## The first real batch, only after the test has passed

1. Clone the sequence as `Project 2: batch 1` with the real waits (3 and 4 days) and step 1 as a Manual Email.
2. Add at most 5 contacts. For each one, write the single sentence that says why you are writing to this person, from something they or their company published.
3. Send each step-1 task by hand after reading it.
4. In HubSpot, set the company's `Signal stage` to Contacted.
5. After 8 days, fill in the table at the end.

## Email drafts, to be rewritten in the owner's voice

Text in `[ ]` is written by hand for each person. Variables are inserted with the **{ }** button.

**Step 1.** Subject: `A work sample for (company)`

> Hi (first name),
>
> I'm looking for a GTM engineering role and would rather show work than send a résumé. [One sentence on why this person, from something they or the company published.]
>
> I built a small pipeline that finds Bay Area AI companies hiring forward deployed engineers, checks SEC filings for recent funding and scores each account with its reasons. The write-up, with its numbers and limits, is here: [link].
>
> Are you the right person to send a short work sample for (company) to? If this isn't relevant, tell me and I won't write again.
>
> [Your name]

**Step 2**, 3 days later, as a reply.

> Hi (first name), one more line in case the first got buried. The two-minute version is the results table at the top of the write-up: [link]. I'm glad to send a sample list for (company) instead of taking your time on a call.
>
> [Your name]

**Step 3**, 4 days later, as a reply.

> Hi (first name), this is my last note. If the timing is wrong or someone else handles this, a one-word reply is enough and I'll leave it there.
>
> [Your name]

## Results, counts only

| Metric | Test | Batch 1 |
|---|---|---|
| Recipients | 2 (own inboxes) | |
| Step 1 sent | | |
| Follow-ups sent automatically | | |
| Replies | | |
| Sequence stopped on reply (yes or no) | | |
| Bounces | | |
| People who asked not to be contacted | | |
| Conversations or interviews | | |

With 30 recipients these numbers describe what happened; they are too few to compare one email version with another.

## Sources

- [Apollo pricing](https://www.apollo.io/pricing): the free plan links Gmail accounts only
- [How to create automated sequences](https://www.apollo.io/blog/create-your-most-successful-sales-sequence-in-apollo-and-streamline-engagement/) (Apollo blog, 2022): New Sequence, Add Step, step types, Wait For
- [Create a Sequence](https://knowledge.apollo.io/hc/en-us/articles/4409231193101-Create-a-Sequence), [Link Your Mailbox to Apollo](https://knowledge.apollo.io/hc/en-us/articles/4409127806093) and [Configure Email Sending Limits](https://knowledge.apollo.io/hc/en-us/articles/4409233349005-Email-Sending-Limits-Overview) (Apollo help center)
- [Create and Use Email Templates](https://knowledge.apollo.io/hc/en-us/articles/4409238165389-Create-and-Edit-Email-Templates) and [Use Basic Dynamic Variables](https://knowledge.apollo.io/hc/en-us/articles/4409415084813-Use-Basic-Snippets) (Apollo help center)
