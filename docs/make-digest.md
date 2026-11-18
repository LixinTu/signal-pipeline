# The weekly digest in Make

A step-by-step build of one automation in [Make](https://www.make.com): every Monday, after the pipeline has run, it reads the companies that are new in HubSpot and writes one digest for the owner. Since step 3 decides uncertain matches by rule, this digest is how the owner sees what the rules did without having to review anything.

**Not used.** The project runs this digest in n8n instead (`digest_n8n.py` and `automation/n8n/weekly_digest.json`), because Make's free plan is reported to have no API, so a scenario there can only be built by hand. This guide stays as the by-hand alternative.

Written on 2026-11-17 and **not run**. Module names come from Make's own documentation as read that day; the fields inside each module are not in the public docs, so if a screen differs from a step below, stop and send a screenshot.

## Ground rules

- **Free plan only.** No card, no paid add-ons. If Make asks for payment, stop.
- **One scenario.** The free plan's limit on active scenarios is reported as 2 by third-party pages and is not on Make's pricing page as read; check the number in your account before building a second one.
- **Run once by hand before scheduling**, and read the credits the run used.
- **Read-only on companies.** The scenario only searches companies and creates one task. It never updates or deletes a company.
- **Nothing personal in screenshots.** Crop the account email, the HubSpot account name and the portal id before a screenshot goes anywhere.

## Limits and budget

| Item | Free plan | Source |
|---|---|---|
| Credits | 1,000 a month | Make's pricing page |
| Shortest interval between scheduled runs | 15 minutes | Make's help page on pricing parameters |
| Active scenarios | 2 | Third-party pages only: check in the account |

One run should cost about the number of new companies plus 3 credits: one for the search, one for each company passed to the aggregator, one for the task. With 10 new companies a week that is about 60 credits a month. **Replace this estimate with the number Make shows after the test run.**

## Why the digest is a HubSpot task and not an email

Make can send email, but for a personal Gmail address its help center requires a custom OAuth client: a Google Cloud project, a consent screen and client credentials. That is a lot of setup for one weekly message, and forum threads report errors even after following it. A HubSpot task needs no third account: Make already has the HubSpot connection, and the task shows up in HubSpot's task list with a due date. A Slack message is the usual alternative and can replace the last step later.

## What it reads

The pipeline writes these company properties (step 4, `push_hubspot.py`):

| Property label in HubSpot | Internal name | Used in the digest for |
|---|---|---|
| Signal stage | `signal_stage` | The filter: only `New` |
| Signal score | `signal_score` | Sorting by eye |
| Signal tier | `signal_tier` | Funded and hiring, or hiring only |
| Signal reason | `signal_reason` | Why it scored |
| Signal flags | `signal_flags` | The rules' doubts; empty for most companies |
| Signal last seen | `signal_last_seen` | When the pipeline last saw it |

`Signal flags` is created by the next live run of `push_hubspot.py`. Until then, leave it out of the digest text.

## Build

1. **Create the account.** Sign up at make.com on the Free plan. Write down the credit balance shown on the dashboard.
2. **New scenario.** Create a scenario and name it `Signal pipeline: weekly digest`.
3. **Module 1: HubSpot CRM → Search for Companies.**
   - Connection: click **Create a connection**, name it, click **Save**, then sign in to HubSpot and confirm access. Make's docs say a HubSpot Super Admin must approve the Make app; in your own account that is you.
   - Filter: `Signal stage` equal to `new`. If the module offers a second filter, add `Create date` after 7 days ago, so the digest lists only this week's companies.
   - Properties to return: company name, domain and the `Signal` properties in the table above.
   - Limit: 50.
4. **Module 2: Tools → Text aggregator.** Source module: the search module. Text, one company per line:
   ```
   {{name}} ({{domain}}): score {{signal_score}}, {{signal_tier}}. Flags: {{signal_flags}}
   ```
   Insert each value by clicking it in Make's mapping panel instead of typing the braces. Row separator: new line.
5. **Module 3: HubSpot CRM → Create a Task.**
   - Title: `Signal digest: new accounts this week`.
   - Body or notes: the aggregated text from module 2.
   - Due date: today. Owner: you. Status: not started.
6. **Test.** Click **Run once**. Check three things:
   - The search module's bubble shows as many bundles as HubSpot shows companies with `Signal stage` = New (5 on 2026-11-17).
   - HubSpot's task list has one new task, and its text names those companies.
   - The run's credit count. Write it down.
7. **Schedule.** Open the scenario's schedule setting and choose **Days of the week**: Monday, 17:00 UTC. The pipeline's GitHub job starts at 15:00 UTC on Mondays, so the digest runs after it. Turn scheduling on.
8. **Export.** In the scenario editor, open the three-dot menu and choose **Export Blueprint**. Save the file as `data/raw/make_weekly_digest_blueprint.json`; git ignores that folder. It may hold account details, so it is checked before anything is published.

## What to report back

- The number of bundles from the search in the test run, and the number of companies with stage New in HubSpot.
- The credits the test run used, and the balance afterwards.
- A screenshot of the finished scenario and one of the task in HubSpot, cropped as above.
- The path of the exported blueprint.
- Any screen that did not match a step.

## If something goes wrong

- **The search returns nothing.** Check the filter value: the stage's internal value is `new`, its label is `New`. Try the other one.
- **`Signal flags` is not in the list of properties.** It does not exist yet in HubSpot; run the build without it.
- **The task has no text.** The aggregator's source module must be the search module.
- **Make shows an error about HubSpot scopes or approval.** Stop and send the message; do not create a developer app.

## Sources

- [Make pricing](https://www.make.com/en/pricing) and [pricing parameters](https://make.com/en/help/general/pricing-parameters)
- [HubSpot CRM app in Make](https://apps.make.com/hubspotcrm): connection, Search for Companies, Create a Task
- [Schedule a scenario](https://help.make.com/schedule-a-scenario) and [Make Academy: scheduling](https://academy-content.make.com/courses/make-basics-modules/05-scheduling-your-scenario)
- [Make Academy: text aggregators](https://academy-content.make.com/courses/make-basics-aggregators/03-text-aggregators-in-action)
- [Connect to Google services using a custom OAuth client](https://help.make.com/connect-to-google-services-using-a-custom-oauth-client)
- [Slack app in Make](https://apps.make.com/slack), for the alternative last step
