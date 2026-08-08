# WizCodes — Cold Email Playbook (target: 250/day, primary inbox)

> The target is 250 sends a day landing in Primary, not Promotions. That is achievable. It is
> not achievable on the current setup, on the current timeline, or for free — and the gap
> between those two facts is what this document exists to close.

---

## 1. Three things that must change before the first send

### 1.1 Brevo stays — so build for what actually gets accounts suspended

Brevo's Anti-Spam Policy formally prohibits scraped contacts and unsolicited email. That is
real, and it is worth knowing you are operating against the letter of it.

But the decision to stay is defensible, because **enforcement in practice is complaint-driven,
not text-driven.** Nobody at Brevo reads your copy and rules on whether it looks scraped. What
trips enforcement is measurable: a bounce rate that says the list is junk, and a complaint
rate that says recipients did not want it. Genuinely researched, genuinely personal outreach
that gets *replies* instead of complaints does not look like a blast in the only metrics the
system watches.

**So the copy standard is not cosmetic — it is the compliance strategy.** Every email must
clear this bar before it sends:

- Opens with a **specific, verifiable observation about that prospect's own site** — the thing
  this system is uniquely able to do, because it has actually assessed the page.
- No merge-field personalisation as a substitute for that. `Hi {{first_name}}` grafted onto an
  identical body is the pattern that generates complaints.
- Reads as one person writing to another about a problem they noticed. No template, no
  header image, no "view in browser".
- Names the sender, the company and the city. `WizCodes, Ahmedabad`.

**The residual risk, stated once so it is a known risk rather than a surprise:** if the
account is suspended, the recovery is a different ESP and a warmed sender identity, and the
list itself is unaffected because it lives in Neon. Keep bounce under 2% and complaints under
0.05% — comfortably inside Brevo's tolerance — and this does not come up.

### 1.2 A subdomain is the free substitute for a second domain

Reputation damage attaches to a domain, so prospecting must not run from the identity that
carries invoices and client threads. Buying separate domains is the textbook fix, but it is
not the only one.

**Send from a subdomain with its own DKIM.** `OUTREACH_FROM_EMAIL=hello@mail.wizcodes.site`
is already set. Verify `mail.wizcodes.site` as a sender in Brevo and publish SPF, DKIM and
DMARC for that subdomain specifically, in the Cloudflare zone the site already uses. Cost:
zero.

**What this buys and what it does not.** Mailbox providers evaluate DKIM-aligned mail largely
at the sending identity, so a subdomain carries most of its own reputation — a bad month on
`mail.wizcodes.site` does not directly become a bad month for `wizcodes.site`. It is not
total insulation: the organisational domain still shares some signal. It is meaningfully
better than sending from the root and it costs nothing, which makes it the right answer under
this constraint.

**What one sending identity does cap is volume.** Safe cold throughput is 100–250/day *per
domain across 3–5 warmed mailboxes*. On one subdomain with one identity, the sustainable
figure is nearer **100–150/day**, and 250 is a stretch that should only be attempted if the
complaint rate is sitting near zero at 150. The agent enforces the ramp and halts on a
threshold breach (§4), so this self-regulates rather than depending on someone watching.

If the numbers stay clean at 150/day and 250 is still wanted, the cheapest next step is a
second free subdomain — `go.wizcodes.site` — warmed separately, not a purchased domain.

### 1.3 The unsubscribe link is a deliverability asset, not a Promotions risk

You are right that Promotions-tab placement kills open rates, and right to be ruthless about
it. But the unsubscribe is not what causes it.

**What actually sorts mail into Promotions:** HTML templates, images, tracking pixels,
multiple links, promotional vocabulary, bulk-identical bodies, and a `List-Unsubscribe` on
mail that otherwise looks like a marketing blast. **Not** the presence of an opt-out.

**What removing it actually does:**

- A recipient who wants out and cannot find a link presses **"Report spam"** instead. Spam
  complaints are the one metric that ends a sending domain. The threshold is **0.30%** and the
  safe target is **under 0.10%** — at 250/day that is roughly **one complaint every three
  days** before you are in trouble. An unsubscribe link is the pressure-release valve that
  keeps complaints off that number.
- CAN-SPAM still requires a working opt-out and a physical postal address in every commercial
  email to a US recipient, whatever the volume. The address is set:
  `WizCodes, Ahmedabad, Gujarat 382443, India`.

**The resolution that gets both things:**

```
List-Unsubscribe: <https://unsub.wizcodeshq.com/u/{token}>, <mailto:unsub@wizcodeshq.com>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

Headers are **invisible to the reader**. Gmail renders them as its own small "Unsubscribe"
control next to the sender name — which is a *trust* signal, not a promotional one. Plus one
plain sentence at the foot of the body:

> Not the right person? Reply "no" and I'll take you off the list. — WizCodes, Ahmedabad,
> Gujarat 382443, India

That reads like a human wrote it, satisfies the law, gives the pressure valve, and adds
nothing that a Promotions classifier keys on.

*(For completeness: Gmail and Yahoo's formal one-click-unsubscribe mandate applies to senders
above 5,000/day, so at 250/day it is not compulsory. It is still the right call, for the
complaint-rate reason above.)*

---

## 2. Getting to 250/day — the ramp

**Day one is not 250.** A brand-new domain sending 250 cold emails on its first day is the
textbook way to get the domain blocklisted before anyone reads a word.

Safe limits: **20–50 per mailbox per day**, **100–250 per domain per day** across 3–5 warmed
mailboxes. So 250/day = **5 mailboxes across 2 domains**, all warmed.

| Phase | Weeks | Per mailbox/day | Total/day | What is happening |
|---|---|---|---|---|
| Warmup | 1–2 | 4 → 10 | 20 → 50 | Domain age + reply-rate signal build |
| Ramp | 3–4 | 10 → 25 | 50 → 125 | Watch complaint + bounce rates daily |
| Scale | 5–6 | 25 → 40 | 125 → 200 | Placement tests weekly |
| Target | 7+ | 50 | **250** | Steady state |

**A new domain needs ~30 days of age before real campaign volume**, regardless of ramp.
Register the outreach domains today even if nothing sends for a month — the clock is the part
you cannot compress.

`MAX_EMAILS_PER_DAY` is therefore not a constant. The agent reads the ramp schedule from
config and refuses to exceed today's cap, so the ramp is enforced by the system rather than by
someone remembering.

---

## 3. Landing in Primary

Ranked by effect.

1. **Plain text. No HTML template, no images, no logo, no signature block with icons.** The
   single biggest lever. It should look like a message typed by one person to another.
2. **One link, maximum. Zero is better on the first touch.** Two or more is the strongest
   Promotions signal after images.
3. **No tracking pixel on the first email.** Open-tracking inserts a 1×1 image from a
   third-party domain — exactly the fingerprint. Track *replies*, which is the only metric
   that matters anyway.
4. **Short.** 50–125 words. A long first email is a broadcast.
5. **Genuinely different per recipient.** The personalisation must be in the *first line and
   the specific observation*, not a `{{first_name}}` merge into an identical body. This system
   has a real advantage here: it has actually looked at the prospect's website and can open
   with something true about it.
6. **Subject lines: 2–4 words, lowercase, no punctuation.** "quick question", "your booking
   page", "noticed on your site". Anything that reads like a headline reads like a campaign.
7. **Send on a human rhythm.** Weekdays, 08:00–11:00 in the *recipient's* timezone, jittered.
   Never a burst — `SEND_JITTER_SECONDS` spreads the batch.
8. **SPF + DKIM + DMARC on every sending domain**, in the Cloudflare zone that already runs
   the site. Start DMARC at `p=none`, move to `p=quarantine` once clean.

**What this rules out:** the HTML-template, image-header, "view in browser" email. That format
is Promotions by construction, and no amount of copy fixes it.

---

## 4. Guard rails that must exist before send #1

| Guard | Threshold | Action on breach |
|---|---|---|
| Bounce rate | > 3% | Halt sending, re-verify the list |
| Spam complaints | > 0.10% | Halt, review copy and targeting |
| Daily cap | ramp schedule | Hard stop, no override |
| MX + SMTP verification | every address | Never send to an unverified guess |
| Suppression check | before draft *and* inside the send transaction | Skip |
| Re-touch cooldown | 90 days per entity | Skip |

Bounces and complaints are the two numbers that decide whether this channel exists in three
months. They belong in the daily Telegram digest, at the top.

---

## 5. Weekly placement test

Once a week, send the live template to a seed panel — a Gmail, an Outlook, a Yahoo, an iCloud
address — and record which tab it landed in. Placement is the only real feedback loop; open
rates on cold email are unreliable and reply rates are too sparse to steer by weekly.

If placement degrades: stop scaling, do not "fix the copy" first. Check volume, complaint rate
and authentication in that order.

---

## 6. Sequence design

**Two emails. Not five.**

- **Touch 1 — day 0.** One specific observation about their site, one sentence on what we'd
  do, one soft ask. No link.
- **Touch 2 — day 6, only if no reply.** Three lines. Adds one new piece of value (a relevant
  `/work/<slug>` page). Then stop, permanently.

Everything past touch 2 converts at a rate that does not justify the complaint risk it adds.
`MAX_SEQUENCE_STEPS=2` enforces it.

---

## 7. The constraint nobody has costed yet — supply

250 sends/day × 22 working days = **5,500 qualified, verified prospects per month.**

That is the harder half of this problem, and it is worth being blunt about:

- **Google Places free tier ≈ 1,000/month.** `websiteUri` is an Enterprise-tier field, so
  every discovery call bills at Enterprise rates. That covers **four days** of sending.
- **OpenStreetMap Overpass is free and unmetered**, and its `website=*` tag gives exactly the
  seed the weak-website assessment needs. At 5,500/month, **OSM has to be the primary source
  and Places the top-up** — the reverse of the original plan.
- Every prospect still needs an assessment (free, PageSpeed) and a verified email (free, MX
  check). Both scale fine.

**Realistic read:** sending capacity reaches 250/day in week 7. Prospect supply is what will
actually cap it. Widen `DISCOVERY_QUERIES` aggressively — more cities, more industries — and
expect supply, not sending, to be the thing you tune.

---

## 8. What is needed from you

| Item | Cost | Why |
|---|---|---|
| Verify `mail.wizcodes.site` as a Brevo sender | free | The sending identity. Brevo shows the exact DNS records to add. |
| SPF / DKIM / DMARC for that subdomain | free | Cloudflare DNS, same zone as the site. DMARC starts at `p=none`. |
| Unsubscribe endpoint | free | One Cloudflare Worker route writing to `core.suppressions` |
| A monitored reply inbox | free | `hello@mail.wizcodes.site` forwarding to your real inbox. Replies must reach a human — this is the one part that cannot be automated away. |
| Google Places key | free to ~1,000/mo | Top-up discovery only; OSM carries the volume |

No purchases. `CHANNELS_ENABLED` currently omits `email` — leave it that way until the
subdomain is verified, DNS is live, and the ramp has run its first fortnight.
