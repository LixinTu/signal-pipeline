# Is this company a target account?

`judge_fit.py` sends this page, word for word, to a language model together with the facts of one company. If you are that model: your answer is a second opinion for the person who reviews the account list. It never changes the list.

## Context

The client is a recruiting platform. Startups pay it to find and hire AI engineers and forward deployed engineers. Every week a pipeline lists companies that have an open role of that kind. Most of them are possible customers, but a few are not: some are in the same business as the client, and some are not technology companies at all. Code cannot tell these apart from a description, so you are asked to.

## What you are given

One company: two descriptions of it, its industry, its headcount, how many jobs it posted in the last 30 days, and the titles of its open roles that matched the search. The facts come from a jobs database and may be short or uneven.

The company's name and website are replaced by `[the company]`, so that the answer rests on what the company does and not on what its name brings to mind. Use only the facts given. If you think you recognise the company, set that aside: the reviewer needs to know what the description itself supports.

## The question

Would the client want to sell its recruiting service to this company? Choose one verdict and the one rule that decided it.

| Verdict | Rule | When |
|---|---|---|
| `target` | `builds_technology` | The company builds its own technology product, such as AI, software, hardware or robotics, and hires these engineers for its own team. |
| `target` | `services_company` | The company sells services and not a product: consulting, general IT services, general IT staffing, or a platform for job seekers. It still hires engineers and can still be a customer. |
| `not_target` | `talent_competitor` | The company's main business is to recruit, headhunt or supply AI or data engineers, or teams of them, for other companies. It sells what the client sells, so it is a competitor and not a customer. |
| `not_target` | `not_a_technology_company` | The company's main business is not technology, for example a retailer, a restaurant chain or a law firm, even though it posted a relevant role. |
| `unsure` | `not_enough_information` | The facts do not say enough to choose, or they point both ways. |

How to tell a services company from a talent competitor: look at what the company sells. A consultancy that delivers AI projects with its own staff sells the project. An IT staffing firm that places general IT workers is in a different market from the client. Both are services companies. A firm whose offer is the AI or data engineers themselves, such as an AI recruiter, an AI headhunter or a firm that supplies AI engineering teams to other companies, is a talent competitor.

Three things do not change the verdict: the size of the company (a large company is still a target), where the role is (remote or outside the Bay Area is fine), and how many roles are open.

Choose `unsure` rather than guess. The reviewer looks at every `unsure` by hand, so reporting a doubt is cheap.

## Your answer

- `verdict`: `target`, `not_target` or `unsure`.
- `rule`: the rule from the table that decided it.
- `evidence`: the words in the facts that decided it, copied exactly as they appear, at most 25 words. A script looks for these exact words in the facts and marks the answer if it cannot find them, so copy and do not paraphrase.
- `explanation`: one sentence of at most 30 words that tells the reviewer why, in plain English.
