#!/usr/bin/env python3
"""Create a self-contained demo database of website-development meetings in Hinglish, plus policy drafts.

Everything goes to separate files, never data/convene.db:

    data/webdev-demo.db              seven ended meetings (Aug to Sep 2026) with speaker-attributed transcripts
    data/webdev-demo.toml            a copy of config/convene.toml pointing at that database (model pins unchanged)
    data/webdev-demo-policies/       where uploaded policy originals will be kept for this database
    data/webdev-demo-exports/        where DOCX minutes will be written for this database
    data/webdev-demo-inputs/*.docx   policy documents drafted from the meetings' decisions, to upload on /policies

The transcripts are written the way Convene's STT stores speech (English plus Hindi in Latin letters, ADR-24) and go
through the same audit events as live speech, so the server indexes them for Q&A at startup. Each meeting also gets a
**hand-written** summary with action items (model_identifier ``demo-seed/hand-written``), so reports and the action-item
list have data without waiting for the local model. Pass ``--no-summaries`` to leave them for the real model.
All names and events are fictional. ``--reset`` deletes exactly the files listed above first.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docx import Document  # noqa: E402

from server import registry  # noqa: E402
from server.action_items import service as action_items  # noqa: E402
from server.audit import emit  # noqa: E402
from server.db import Database  # noqa: E402
from server.ids import new_id  # noqa: E402
from server.repositories import audit_events, summaries, utterances  # noqa: E402
from server.repositories.models import Utterance  # noqa: E402

DATA = ROOT / "data"
DB = DATA / "webdev-demo.db"
CONFIG = DATA / "webdev-demo.toml"
POLICY_DIR = DATA / "webdev-demo-policies"
EXPORT_DIR = DATA / "webdev-demo-exports"
INPUT_DIR = DATA / "webdev-demo-inputs"
SEED_MODEL = "demo-seed/hand-written"

# --- the team -----------------------------------------------------------------------------------------------------
# Aditya: tech lead · Priya: frontend · Rohan: backend · Sneha: QA · Karan: DevOps · Neha: product and design

MEETINGS = [
    {
        "title": "Kickoff: Bloomleaf website redesign",
        "start": "2026-08-18T05:00:00Z",
        "people": ["Aditya", "Neha", "Priya", "Rohan"],
        "lines": [
            ("Neha", "Okay sab log aa gaye, let's start. Client Bloomleaf Organics hai, unki current website WordPress pe hai aur bahut slow hai."),
            ("Neha", "Unka main goal hai online orders badhana. Abhi mobile pe checkout complete karne mein log drop ho jaate hain."),
            ("Aditya", "Humne discuss kiya tha, hum Next.js pe migrate karenge with a headless CMS. Content team ko Sanity dena hai."),
            ("Priya", "Next.js ka App Router use karenge na? Mujhe lagta hai server components se product pages kaafi fast ho jayenge."),
            ("Aditya", "Haan, App Router. Styling ke liye Tailwind, aur design tokens Figma se export honge."),
            ("Rohan", "Backend side pe orders ke liye ek Node API banega, Postgres ke saath. Payments Razorpay se hoga."),
            ("Neha", "Timeline ye hai ki beta 30 September ko client ko dikhana hai, aur public launch 15 October."),
            ("Priya", "Thoda tight hai but doable hai agar designs is week ke end tak final ho jaayein."),
            ("Neha", "Designs Friday tak final kar dungi, homepage aur product page pehle."),
            ("Aditya", "Ek baat clear kar dete hain: har feature ka ek owner hoga aur Jira ticket ke bina koi kaam start nahi hoga."),
            ("Rohan", "Staging environment kab tak chahiye? Mujhe API test karne ke liye jaldi chahiye."),
            ("Aditya", "Karan next week tak staging set kar dega. Main usse bol dunga."),
            ("Priya", "Aur browsers? Client ke customers mostly Android Chrome pe hain, Safari bhi support karna padega."),
            ("Neha", "Latest two versions of Chrome, Safari, Firefox aur Edge. Internet Explorer bilkul nahi."),
            ("Aditya", "Theek hai. To summary: Next.js plus Sanity, Node API, beta 30 September, launch 15 October. Chalo, kaam shuru karte hain."),
        ],
        "summary": ("The team kicked off the Bloomleaf Organics website redesign. The client's current WordPress site is "
                    "slow, and mobile shoppers drop out during checkout, so the goal is to increase online orders.\n\n"
                    "The stack was agreed: Next.js with the App Router and Tailwind on the frontend, Sanity as the "
                    "headless CMS, and a Node API with Postgres and Razorpay for orders. The beta is due for the client "
                    "on 30 September and the public launch on 15 October. Every feature needs an owner and a Jira "
                    "ticket before work starts. Supported browsers are the latest two versions of Chrome, Safari, "
                    "Firefox and Edge."),
        "items": [("Finalize homepage and product page designs by Friday", "Neha", "done", "2026-08-22"),
                  ("Set up the staging environment", "Aditya", "done", "2026-08-26"),
                  ("Create the Next.js project skeleton with Tailwind", "Priya", "done", None),
                  ("Design the orders API and Postgres schema", "Rohan", "done", None)],
    },
    {
        "title": "Git workflow and code review rules",
        "start": "2026-08-25T06:30:00Z",
        "people": ["Aditya", "Priya", "Rohan", "Sneha"],
        "lines": [
            ("Aditya", "Pichle hafte do baar main branch pe direct push hua aur staging toot gaya. Isliye aaj rules fix karte hain."),
            ("Rohan", "Haan woh meri galti thi, hotfix tha isliye seedha push kar diya."),
            ("Aditya", "Koi baat nahi, but ab se main branch protected rahegi. Direct push kisi ka bhi allowed nahi hoga, mera bhi nahi."),
            ("Priya", "Branch naming ka bhi ek format rakhte hain. Feature ke liye feature slash ticket number, jaise feature/BL-42-cart-page."),
            ("Aditya", "Aur bug ke liye fix slash ticket number, urgent production issue ke liye hotfix slash."),
            ("Sneha", "Pull request mein testing steps likhna compulsory karo please. Mujhe har baar poochna padta hai kaise test karun."),
            ("Aditya", "Agreed. PR template banayenge: what changed, screenshots for UI, aur how to test."),
            ("Rohan", "Review kitne log karenge? Ek approval kaafi hai?"),
            ("Aditya", "Minimum ek approval, aur agar payments ya auth ka code hai toh do approvals, jisme ek mera ya Rohan ka hoga."),
            ("Priya", "Review ka time bhi fix karo. PR do din pada rehta hai toh flow toot jaata hai."),
            ("Aditya", "Theek hai, review 24 working hours ke andar hona chahiye. Nahi ho paye toh standup mein bolo."),
            ("Sneha", "Aur CI green hona chahiye merge se pehle. Lint, type check aur unit tests."),
            ("Aditya", "Haan, CI fail hai toh merge button disabled rahega. Squash merge karenge taaki history clean rahe."),
            ("Rohan", "Commit messages ke liye conventional commits use karein? feat, fix, chore wagera."),
            ("Aditya", "Haan, conventional commits. Main ye sab ek document mein likh deta hoon, isko policy bana dete hain."),
        ],
        "summary": ("After two direct pushes to main broke staging, the team agreed a Git workflow. The main branch is "
                    "protected and nobody may push to it directly. Branches are named feature/<ticket>-<name>, "
                    "fix/<ticket>-<name> or hotfix/<ticket>-<name>.\n\n"
                    "Every pull request uses a template with what changed, screenshots for UI changes, and testing "
                    "steps. At least one approval is required, and two for payments or authentication code, one of "
                    "them from Aditya or Rohan. Reviews should happen within 24 working hours. CI (lint, type check, "
                    "unit tests) must pass before merging, merges are squashed, and commit messages follow "
                    "conventional commits. Aditya will write this up as a policy."),
        "items": [("Enable branch protection on main", "Aditya", "done", "2026-08-26"),
                  ("Add the pull request template", "Priya", "done", None),
                  ("Write the Git workflow policy document", "Aditya", "done", "2026-08-29"),
                  ("Make CI block merges when checks fail", "Rohan", "done", None)],
    },
    {
        "title": "Deployment and release process",
        "start": "2026-09-03T09:00:00Z",
        "people": ["Aditya", "Karan", "Rohan", "Sneha"],
        "lines": [
            ("Karan", "Staging ready hai. Har merge to main pe automatically staging pe deploy hoga, Vercel pe frontend aur Railway pe API."),
            ("Aditya", "Badhiya. Ab production release ka process decide karte hain."),
            ("Karan", "Mera suggestion hai production deploy manual approval ke saath ho, sirf Aditya ya main approve kar sakte hain."),
            ("Sneha", "Release se pehle staging pe mera smoke test pass hona chahiye. Checkout, login aur search, ye teen must hain."),
            ("Rohan", "Database migrations ka kya? Agar migration fail hua toh rollback kaise karenge?"),
            ("Karan", "Har migration ka down script hona chahiye, aur production migration se pehle database ka backup lenge."),
            ("Aditya", "Aur Friday ko production deploy nahi karenge. Weekend pe koi issue aaya toh koi available nahi hota."),
            ("Rohan", "Friday poora band? Thursday shaam ke baad bhi risky hai."),
            ("Aditya", "Chalo, Friday aur weekend pe freeze. Critical hotfix ho toh sirf mere approval se."),
            ("Karan", "Rollback ke liye Vercel mein previous deployment instantly promote ho jaata hai. API ke liye bhi previous image rakhenge."),
            ("Sneha", "Release notes bhi chahiye, client ko batana padta hai kya change hua."),
            ("Karan", "Main ek release checklist bana deta hoon: smoke test, backup, migration, deploy, monitoring check."),
            ("Aditya", "Deploy ke baad pehle 30 minute tak koi ek banda error dashboard dekhega. Sentry alerts on rahenge."),
            ("Karan", "Theek hai, isko bhi policy mein daal dete hain. Deployment policy version one."),
        ],
        "summary": ("Staging now deploys automatically on every merge to main (frontend on Vercel, API on Railway). "
                    "Production deploys need manual approval from Aditya or Karan, and Sneha's staging smoke test of "
                    "checkout, login and search must pass first.\n\n"
                    "Every database migration needs a down script, and production is backed up before migrating. "
                    "There are no production deploys on Friday or the weekend except critical hotfixes approved by "
                    "Aditya. Rollback uses Vercel's previous deployment and the previous API image. Each release gets "
                    "release notes for the client and a checklist, and someone watches Sentry for 30 minutes after "
                    "deploying. Karan will write this up as the deployment policy."),
        "items": [("Write the release checklist and deployment policy", "Karan", "done", "2026-09-08"),
                  ("Add down scripts to all existing migrations", "Rohan", "done", None),
                  ("Write the staging smoke test for checkout, login and search", "Sneha", "done", "2026-09-10"),
                  ("Set up Sentry alerts for the production API", "Karan", "open", "2026-10-02")],
    },
    {
        "title": "Accessibility and performance standards",
        "start": "2026-09-10T05:30:00Z",
        "people": ["Neha", "Priya", "Sneha", "Aditya"],
        "lines": [
            ("Neha", "Client ne bola hai unke customers mein kaafi senior citizens hain, toh accessibility pe dhyaan dena padega."),
            ("Priya", "Hum WCAG 2.1 AA target karte hain. Colour contrast minimum 4.5 is to 1 normal text ke liye."),
            ("Sneha", "Keyboard se poori site chalni chahiye. Abhi cart drawer keyboard se band hi nahi hota."),
            ("Priya", "Haan woh fix karungi, focus trap lagana padega. Aur har image pe meaningful alt text."),
            ("Neha", "Product images ke alt text CMS mein content team likhegi, main unko guideline de dungi."),
            ("Aditya", "Performance ke liye bhi number fix karo. Mobile pe Lighthouse performance score kam se kam 90."),
            ("Priya", "Largest Contentful Paint 2.5 second se kam, aur layout shift 0.1 se kam. Core Web Vitals ke hisaab se."),
            ("Sneha", "Abhi homepage ka hero image 2 MB ka hai, isliye LCP 4 second aa raha hai."),
            ("Priya", "Next Image component use karenge, WebP ya AVIF mein, aur hero image 200 KB se zyada nahi honi chahiye."),
            ("Aditya", "Fonts bhi self host karo, Google Fonts ka external call hatao."),
            ("Neha", "Aur forms mein error message sirf red colour se nahi, text mein bhi likha hona chahiye."),
            ("Sneha", "Main release checklist mein axe scan aur Lighthouse check add kar deti hoon. Score 90 se kam hua toh release block."),
            ("Aditya", "Perfect. Ye frontend standards policy ban jaayegi. Priya, draft tum likh do."),
        ],
        "summary": ("Many of the client's customers are older, so the site targets WCAG 2.1 AA: 4.5:1 contrast for body "
                    "text, full keyboard navigation (the cart drawer needs a focus trap), meaningful alt text on every "
                    "image, and form errors stated in text, not only in colour.\n\n"
                    "Performance targets on mobile are a Lighthouse performance score of at least 90, LCP under 2.5 "
                    "seconds and CLS under 0.1. The homepage hero image is 2 MB and causes a 4 second LCP, so images "
                    "use the Next Image component in WebP or AVIF, with the hero under 200 KB, and fonts are "
                    "self-hosted. An axe scan and a Lighthouse check join the release checklist, and a score under 90 "
                    "blocks the release. Priya will draft the frontend standards policy."),
        "items": [("Fix keyboard focus trap in the cart drawer", "Priya", "done", "2026-09-15"),
                  ("Compress the homepage hero image under 200 KB", "Priya", "done", None),
                  ("Write alt-text guidelines for the content team", "Neha", "open", "2026-09-25"),
                  ("Add axe and Lighthouse checks to the release checklist", "Sneha", "done", None),
                  ("Draft the frontend standards policy", "Priya", "done", "2026-09-17")],
    },
    {
        "title": "Incident review: leaked API key",
        "start": "2026-09-17T10:00:00Z",
        "people": ["Aditya", "Karan", "Rohan"],
        "lines": [
            ("Aditya", "Kal raat GitHub ne alert bheja ki Razorpay ka test key ek commit mein push ho gaya tha. Chalo isko review karte hain."),
            ("Rohan", "Haan, meri .env file galti se commit ho gayi thi. Gitignore mein .env.local tha, sirf .env nahi tha."),
            ("Karan", "Maine key turant rotate kar di thi, aur purani key disable ho gayi hai. Koi transaction nahi hua uss key se."),
            ("Aditya", "Good. Blame nahi karna hai, process fix karna hai. Pehli baat, koi bhi secret repo mein nahi jaayega, test key bhi nahi."),
            ("Karan", "Saare secrets Vercel aur Railway ke environment variables mein rahenge, aur team ke liye 1Password vault."),
            ("Rohan", "Pre-commit hook laga dete hain, gitleaks, jo secret pakad le commit se pehle hi."),
            ("Aditya", "Haan, aur CI mein bhi secret scan chalega, taaki hook bypass kiya toh bhi pakda jaaye."),
            ("Karan", "Repo mein sirf .env.example rahega, bina real values ke."),
            ("Aditya", "Agar phir kabhi leak ho, toh ek ghante ke andar key rotate karni hai aur mujhe aur Karan ko inform karna hai."),
            ("Rohan", "Production keys ka access bhi limit karo. Abhi sabke paas hai."),
            ("Karan", "Production secrets sirf mere aur Aditya ke paas rahenge. Baaki sab staging keys use karenge."),
            ("Aditya", "Aur har teen mahine mein production keys rotate karenge. Karan, ye sab security policy mein likh do."),
        ],
        "summary": ("A Razorpay test key was pushed to the repository in a committed .env file because .gitignore "
                    "covered only .env.local. Karan rotated the key immediately and no transaction used it. The "
                    "review was blameless and focused on fixing the process.\n\n"
                    "No secret, including test keys, may be committed. Secrets live in Vercel and Railway environment "
                    "variables and a shared 1Password vault, and the repository holds only .env.example without real "
                    "values. A gitleaks pre-commit hook and a CI secret scan catch leaks. A leaked key must be rotated "
                    "within one hour and reported to Aditya and Karan. Only Aditya and Karan hold production secrets, "
                    "and production keys are rotated every three months. Karan will write the security policy."),
        "items": [("Add .env to .gitignore and commit .env.example", "Rohan", "done", None),
                  ("Add the gitleaks pre-commit hook and CI secret scan", "Rohan", "done", "2026-09-19"),
                  ("Move production secrets to the 1Password vault and restrict access", "Karan", "done", None),
                  ("Write the secrets and security policy", "Karan", "open", "2026-09-24")],
    },
    {
        "title": "Beta readiness review",
        "start": "2026-09-24T07:00:00Z",
        "people": ["Neha", "Aditya", "Priya", "Rohan", "Sneha"],
        "lines": [
            ("Neha", "Beta 30 September ko hai, toh aaj dekhte hain kya ready hai aur kya nahi."),
            ("Priya", "Homepage, product listing aur product page ready hain. Mobile Lighthouse score 93 aa raha hai."),
            ("Rohan", "Orders API aur Razorpay test mode integration done hai. Refund wala flow abhi baaki hai."),
            ("Sneha", "Checkout mein ek bug hai, coupon lagane ke baad total refresh nahi hota. Maine ticket BL-118 banaya hai."),
            ("Rohan", "Woh main kal tak fix kar dunga, backend se updated total hi nahi aa raha."),
            ("Neha", "Refund flow beta ke liye zaroori nahi hai, client ko main bata dungi ki woh launch se pehle aayega."),
            ("Aditya", "Theek hai. Beta ke liye scope: browse, cart, checkout with coupons. Refund launch tak."),
            ("Sneha", "Safari pe ek aur issue hai, sticky header scroll karte waqt flicker karta hai."),
            ("Priya", "Haan dekha maine, CSS backdrop filter ki wajah se hai. Aaj fix ho jaayega."),
            ("Aditya", "Deployment policy ke hisaab se beta Tuesday 29 ko deploy karenge, Friday nahi."),
            ("Neha", "Client demo 30 ko 11 baje hai. Main demo script bana leti hoon."),
            ("Aditya", "Aur ek cheez: security policy abhi tak final nahi hui. Karan se bolo launch se pehle final kare."),
            ("Neha", "Done. To beta go hai, bas coupon bug aur Safari header fix hone chahiye."),
        ],
        "summary": ("The team reviewed readiness for the 30 September beta. The homepage, product listing and product "
                    "page are ready with a mobile Lighthouse score of 93. The orders API and Razorpay test-mode "
                    "integration are done, and the refund flow moves to before launch.\n\n"
                    "Two bugs block the beta: the checkout total does not refresh after a coupon is applied (BL-118), "
                    "and the sticky header flickers in Safari. Beta scope is browsing, cart, and checkout with coupons. "
                    "Following the deployment policy, the beta deploys on Tuesday 29 September, not Friday. The client "
                    "demo is on 30 September at 11:00. The security policy still needs to be finalized before launch."),
        "items": [("Fix coupon total not refreshing at checkout (BL-118)", "Rohan", "done", "2026-09-25"),
                  ("Fix sticky header flicker in Safari", "Priya", "done", "2026-09-24"),
                  ("Build the refund flow before launch", "Rohan", "open", "2026-10-10"),
                  ("Prepare the client beta demo script", "Neha", "open", "2026-09-29"),
                  ("Deploy the beta to production on 29 September", "Aditya", "open", "2026-09-29")],
    },
    {
        "title": "Bug triage and on-call rota",
        "start": "2026-09-28T08:30:00Z",
        "people": ["Sneha", "Karan", "Aditya", "Priya"],
        "lines": [
            ("Sneha", "Is hafte 14 naye bugs aaye hain. Priority kaise decide karein, ek system chahiye."),
            ("Aditya", "Chaar levels rakhte hain. P1 matlab site down ya payment fail, P2 matlab koi major feature toota, P3 minor, P4 cosmetic."),
            ("Karan", "P1 pe response 30 minute ke andar hona chahiye, chahe raat ho. Isliye on-call rota chahiye."),
            ("Priya", "Rota weekly rakhte hain? Ek hafta ek developer, backup ke saath."),
            ("Karan", "Haan weekly, Monday se Monday. Pehla hafta main le leta hoon, Rohan backup."),
            ("Sneha", "P2 ka fix do working days mein, P3 current sprint mein, aur P4 backlog mein."),
            ("Aditya", "Aur har P1 ke baad 48 ghante mein ek blameless incident review hoga, jaise humne API key wale case mein kiya."),
            ("Priya", "Bug report mein kya kya hona chahiye? Aadhe reports mein steps hi nahi hote."),
            ("Sneha", "Template banati hoon: steps to reproduce, expected, actual, browser aur device, aur screenshot."),
            ("Karan", "PagerDuty ka free plan le lete hain, P1 alerts phone pe aayenge."),
            ("Aditya", "Sahi hai. Ye incident aur bug policy ho jaayegi. Sneha, draft tum likho."),
        ],
        "summary": ("The team set up bug priorities and an on-call rota. P1 means the site is down or payments fail, P2 "
                    "a major feature is broken, P3 minor, P4 cosmetic. P1 needs a response within 30 minutes at any "
                    "hour, P2 a fix within two working days, P3 within the current sprint, and P4 goes to the "
                    "backlog.\n\n"
                    "On-call rotates weekly, Monday to Monday, with a backup; Karan takes the first week with Rohan as "
                    "backup. Every P1 gets a blameless incident review within 48 hours. Bug reports follow a template "
                    "with steps to reproduce, expected and actual results, browser and device, and a screenshot. P1 "
                    "alerts go to phones through PagerDuty. Sneha will draft the incident and bug policy."),
        "items": [("Create the bug report template", "Sneha", "done", None),
                  ("Set up PagerDuty for P1 alerts", "Karan", "open", "2026-10-01"),
                  ("Publish the on-call rota for October", "Karan", "open", "2026-09-30"),
                  ("Draft the incident and bug policy", "Sneha", "open", "2026-10-03")],
    },
]

# Action items closed later than their meeting: (meeting index, item index, when). The rest stay open, or are marked
# done right after their meeting.
CLOSED_LATER = {(0, 0): "2026-08-22T12:00:00Z", (0, 1): "2026-08-27T12:00:00Z", (0, 2): "2026-08-21T12:00:00Z",
                (0, 3): "2026-08-24T12:00:00Z", (1, 0): "2026-08-26T09:00:00Z", (1, 1): "2026-08-27T09:00:00Z",
                (1, 2): "2026-08-29T15:00:00Z", (1, 3): "2026-08-28T15:00:00Z", (2, 0): "2026-09-08T11:00:00Z",
                (2, 1): "2026-09-07T11:00:00Z", (2, 2): "2026-09-09T11:00:00Z", (3, 0): "2026-09-14T11:00:00Z",
                (3, 1): "2026-09-12T11:00:00Z", (3, 3): "2026-09-13T11:00:00Z", (3, 4): "2026-09-18T11:00:00Z",
                (4, 0): "2026-09-17T13:00:00Z", (4, 1): "2026-09-19T13:00:00Z", (4, 2): "2026-09-18T13:00:00Z",
                (5, 0): "2026-09-25T10:00:00Z", (5, 1): "2026-09-24T16:00:00Z", (6, 0): "2026-09-29T10:00:00Z"}

POLICIES = {
    "git-workflow-policy.docx": ("Git Workflow and Code Review Policy", [
        ("Scope", "Applies to every repository of the Bloomleaf website project."),
        ("Branches", "The main branch is protected. Nobody may push to main directly, including the tech lead. "
                     "Branch names are feature/<ticket>-<name>, fix/<ticket>-<name> or hotfix/<ticket>-<name>, for "
                     "example feature/BL-42-cart-page. Work starts only from a Jira ticket with an owner."),
        ("Pull requests", "Every pull request uses the template: what changed, screenshots for UI changes, and how to "
                          "test. At least one approval is required. Changes to payments or authentication need two "
                          "approvals, one of them from the tech lead or the backend lead."),
        ("Review time", "Reviews are done within 24 working hours. A pull request waiting longer is raised at standup."),
        ("Merging", "Lint, type check and unit tests must pass in CI before merging; the merge button stays disabled "
                    "otherwise. Pull requests are squash-merged. Commit messages follow conventional commits (feat, "
                    "fix, chore, docs, refactor, test)."),
    ]),
    "deployment-policy-v1.docx": ("Deployment and Release Policy (version 1)", [
        ("Environments", "Every merge to main deploys to staging automatically. Production deploys require manual "
                         "approval by the tech lead or the DevOps engineer."),
        ("Before a release", "The staging smoke test for checkout, login and search must pass. Production is backed "
                             "up before any database migration, and every migration has a down script."),
        ("Release freeze", "No production deploys on Fridays, Saturdays or Sundays. A critical hotfix during the "
                           "freeze needs the tech lead's approval."),
        ("After a release", "Release notes go to the client. One engineer watches Sentry for 30 minutes after the "
                            "deploy. Rollback uses the previous Vercel deployment and the previous API image."),
    ]),
    "deployment-policy-v2.docx": ("Deployment and Release Policy (version 2)", [
        ("Environments", "Every merge to main deploys to staging automatically. Production deploys require manual "
                         "approval by the tech lead or the DevOps engineer."),
        ("Before a release", "The staging smoke test for checkout, login and search must pass, and the mobile "
                             "Lighthouse performance score must be at least 90 with no axe accessibility errors. "
                             "Production is backed up before any database migration, and every migration has a down "
                             "script."),
        ("Release freeze", "No production deploys from Thursday 18:00 IST until Monday 10:00 IST. A critical hotfix "
                           "during the freeze needs the tech lead's approval and an on-call engineer present."),
        ("After a release", "Release notes go to the client within one working day. The on-call engineer watches "
                            "Sentry for 60 minutes after the deploy. Rollback uses the previous Vercel deployment "
                            "and the previous API image, and must be possible within 10 minutes."),
    ]),
    "frontend-standards-policy.docx": ("Frontend Accessibility and Performance Standards", [
        ("Accessibility", "The site meets WCAG 2.1 AA. Body text has a contrast ratio of at least 4.5:1. The whole "
                          "site works with a keyboard, and dialogs and drawers trap focus. Every meaningful image has "
                          "alt text written by the content team. Form errors are stated in text, never by colour "
                          "alone."),
        ("Performance", "On mobile: Lighthouse performance score of at least 90, Largest Contentful Paint under 2.5 "
                        "seconds, Cumulative Layout Shift under 0.1."),
        ("Images and fonts", "Images use the Next Image component in WebP or AVIF. The hero image is at most 200 KB. "
                             "Fonts are self-hosted; no external font requests."),
        ("Browser support", "The latest two versions of Chrome, Safari, Firefox and Edge. Internet Explorer is not "
                            "supported."),
        ("Enforcement", "Every release runs an axe scan and a Lighthouse check. A performance score under 90 blocks "
                        "the release."),
    ]),
    "security-secrets-policy.docx": ("Secrets and Security Policy", [
        ("Secrets in code", "No secret may be committed to a repository, including test keys. Repositories contain "
                            "only .env.example with placeholder values; .env and .env.local are ignored."),
        ("Where secrets live", "Secrets live in Vercel and Railway environment variables and the team 1Password "
                               "vault. Only the tech lead and the DevOps engineer hold production secrets; everyone "
                               "else uses staging keys."),
        ("Detection", "A gitleaks pre-commit hook runs on every developer machine, and CI runs a secret scan on every "
                      "pull request."),
        ("If a secret leaks", "Rotate the key within one hour and inform the tech lead and the DevOps engineer. A "
                              "blameless incident review follows within 48 hours."),
        ("Rotation", "Production keys are rotated every three months."),
    ]),
    "incident-bug-policy.docx": ("Incident and Bug Priority Policy", [
        ("Priorities", "P1: the site is down or payments fail. P2: a major feature is broken. P3: a minor issue. "
                       "P4: cosmetic."),
        ("Response times", "P1: response within 30 minutes at any hour. P2: fixed within two working days. P3: fixed "
                           "within the current sprint. P4: added to the backlog."),
        ("On-call", "On-call rotates weekly from Monday to Monday, with a named backup. P1 alerts reach the on-call "
                    "engineer's phone through PagerDuty."),
        ("Incident review", "Every P1 gets a blameless incident review within 48 hours."),
        ("Bug reports", "Every bug report includes steps to reproduce, expected result, actual result, browser and "
                        "device, and a screenshot."),
    ]),
}


def ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed_meeting(db, spec: dict, with_summary: bool) -> tuple[str, list[str], datetime]:
    """One meeting through the same records as a live one; returns (meeting_id, item ids, end time)."""
    start = parse(spec["start"])
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, spec["title"], now=ts(start - timedelta(minutes=3)))
        people, device_of = {}, {}
        for n, name in enumerate(spec["people"]):
            registration = registry.register_device(tx, meeting.meeting_id, new_id(), name,
                                                    now=ts(start - timedelta(minutes=2, seconds=-n * 10)))
            people[name] = registration.participants[0].participant_id
            device_of[name] = registration.device.device_id
        for n, name in enumerate(spec["people"]):
            registry.record_device_connected(tx, device_of[name], now=ts(start + timedelta(seconds=n * 5)))
        clock = start + timedelta(seconds=30)
        for speaker, text in spec["lines"]:
            spoken = max(2.5, len(text.split()) * 0.42)
            t_end = clock + timedelta(seconds=spoken)
            created = ts(t_end + timedelta(seconds=1.2))
            row = utterances.insert(tx.conn, Utterance(new_id(), meeting.meeting_id, device_of[speaker],
                                                       people[speaker], text, ts(clock), ts(t_end), 0.91, "device",
                                                       0.95, created))
            emit(tx, "utterance_created", "attribution", {
                "utterance_id": row.utterance_id, "device_id": row.device_id, "participant_id": row.participant_id,
                "attribution_method": "device", "attribution_confidence": 0.95, "stt_confidence": 0.91},
                meeting_id=meeting.meeting_id, timestamp=created)
            clock = t_end + timedelta(seconds=2.5)
        end = clock + timedelta(minutes=1)
        registry.end_meeting(tx, meeting.meeting_id, now=ts(end))
        item_ids = []
        if with_summary:
            summary_id, generated = new_id(), ts(end + timedelta(minutes=2))
            summaries.insert_pending(tx.conn, summary_id=summary_id, meeting_id=meeting.meeting_id,
                                     model_identifier=SEED_MODEL)
            summaries.finish(tx.conn, summary_id, status="ready", generated_at=generated, summary_text=spec["summary"])
            for text, owner, _status, due in spec["items"]:
                item = summaries.ActionItem(new_id(), summary_id, meeting.meeting_id, text, people.get(owner), "open", due)
                summaries.insert_action_item(tx.conn, item)
                item_ids.append(item.action_item_id)
            emit(tx, "summary_generated", "summary", {
                "summary_id": summary_id, "input_as_of_seq": audit_events.max_seq(tx.conn),
                "model_identifier": SEED_MODEL, "action_item_count": len(item_ids), "attempts": 1,
                "duration_ms": 0, "drain_timed_out": False}, meeting_id=meeting.meeting_id, timestamp=generated)
    return meeting.meeting_id, item_ids, end


def close_item(db, action_item_id: str, status: str, at: str) -> None:
    """A manual status edit, recorded exactly as the UI records it, dated when it happened."""
    with db.transaction() as tx:
        action_items.update_action_item(tx, action_item_id, {"status": status})
        tx.conn.execute("UPDATE AuditEvent SET timestamp = ? WHERE seq = ?", (at, audit_events.max_seq(tx.conn)))


def write_policies() -> list[Path]:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (title, sections) in POLICIES.items():
        document = Document()
        document.add_heading(title, 0)
        document.add_paragraph("Bloomleaf website project · Fictional demo document drafted from team meetings.")
        for heading, body in sections:
            document.add_heading(heading, level=1)
            document.add_paragraph(body)
        path = INPUT_DIR / name
        document.save(path)
        paths.append(path)
    return paths


def write_config() -> None:
    """The normal config with only the storage locations changed, so model pins stay identical."""
    text = (ROOT / "config" / "convene.toml").read_text(encoding="utf-8")
    for key, value in (("database", DB), ("exports", EXPORT_DIR), ("policies", POLICY_DIR), ("storage_dir", POLICY_DIR)):
        text, count = re.subn(rf'^{key}\s*=\s*"[^"]*"', f'{key} = "{value.resolve()}"', text, count=1, flags=re.MULTILINE)
        if count != 1:
            raise SystemExit(f"config/convene.toml has no `{key} = ...` line to replace")
    CONFIG.write_text(text, encoding="utf-8")


def reset() -> None:
    for path in (DB, DB.with_name(DB.name + "-wal"), DB.with_name(DB.name + "-shm"), CONFIG):
        path.unlink(missing_ok=True)
    for directory in (POLICY_DIR, EXPORT_DIR, INPUT_DIR):
        if directory.is_dir():
            shutil.rmtree(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the previous webdev demo files first")
    parser.add_argument("--no-summaries", action="store_true",
                        help="do not seed hand-written summaries; generate them with the local model instead")
    args = parser.parse_args()
    if args.reset:
        reset()
    if DB.exists():
        raise SystemExit(f"{DB} already exists; run with --reset to recreate it")
    DATA.mkdir(parents=True, exist_ok=True)
    db = Database.open(DB)
    try:
        closures = []
        for m, spec in enumerate(MEETINGS):
            _, item_ids, end = seed_meeting(db, spec, not args.no_summaries)
            for i, item_id in enumerate(item_ids):
                if spec["items"][i][2] != "open":
                    closures.append((ts(parse(CLOSED_LATER[(m, i)])) if (m, i) in CLOSED_LATER else ts(end + timedelta(hours=3)), item_id, spec["items"][i][2]))
        for at, item_id, status in sorted(closures):
            close_item(db, item_id, status, at)
        meetings = db.conn.execute("SELECT COUNT(*) FROM Meeting").fetchone()[0]
        lines = db.conn.execute("SELECT COUNT(*) FROM Utterance").fetchone()[0]
        items = db.conn.execute("SELECT COUNT(*) FROM ActionItem").fetchone()[0]
    finally:
        db.close()
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    policies = write_policies()
    write_config()
    print(f"Created {DB.relative_to(ROOT)}: {meetings} ended meetings, {lines} transcript lines, {items} action items"
          + (" (summaries are hand-written seeds)" if items else " (no summaries: press Summarize on each meeting)"))
    print(f"Config: {CONFIG.relative_to(ROOT)}")
    print(f"Policy drafts to upload on /policies ({len(policies)}): {INPUT_DIR.relative_to(ROOT)}/")
    print(f"Start: .venv/bin/python -m server.app --config {CONFIG.relative_to(ROOT)} --cert <cert> --key <key> ...")


if __name__ == "__main__":
    main()
