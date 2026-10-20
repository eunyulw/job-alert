# Company source gaps and fallbacks

Every company here is wanted but has no working source with a verified company identity. Each entry
gives the specific reason. Update this list on every expansion pass: remove a company when a source
goes live, and add every company that was probed and missed.

Robotics, mechanical, aerospace and automotive companies come first, because those are the search's
priority. Last updated 2026-10-20, after the user's robotics list added 16 direct
sources, and after the freehire company-feed fallback pass (see
[collection-verification.md](collection-verification.md)), which moved 55 more companies to enabled
`FALLBACK_ONLY` sources. The board-catalog pass on 2026-10-18 moved 70 companies to direct sources.

A company whose careers site refuses us can still be reachable when its job board is published
somewhere else that answers an ordinary request. The fallback uses Freehire's keyless public API
(github.com/strelov1/freehire): each source names one exact Freehire company slug, requires every
returned record to carry that same slug, and keeps the original source provider and direct URL.
This monitor does not use Freehire's TLS-fingerprint spoofing, residential proxy, headless-browser,
or hosted-scraping tiers.

## 1. Reached through Freehire's public company feed

These sources are enabled at `FALLBACK_ONLY` and poll every 60 minutes. The first group was listed
as provider-refused in the previous pass.

- **Robotics, industrial and mobility:** Tesla, Lockheed Martin, Eaton, Whirlpool, Lam Research,
  Qualcomm, OMRON, CMR Surgical, Mercedes-Benz Group, Ferrari, SAP.
- **Big tech and semiconductors:** Adobe, Broadcom, Cisco, Cloudflare, Microsoft, Snowflake,
  Cadence, OpenAI, ServiceNow, Synopsys, TSMC, Tencent, xAI.
- **Fortune 100:** AbbVie, Archer Daniels Midland, CVS Health, Cencora, Cigna Group, Freddie Mac,
  Humana, Lowe's, Merck, Performance Food Group, Procter & Gamble, Sysco, TJX, UPS, USAA, United
  Airlines, Albertsons, Bristol-Myers Squibb, Centene, ConocoPhillips, Eli Lilly, Fannie Mae,
  Liberty Mutual, Marathon Petroleum, Nationwide, Pfizer, Progressive, StoneX Group, Valero Energy,
  Verizon and Wells Fargo.

## 2. Still refused with no exact Freehire company feed

| Company | What the provider answers | Why the fallback is not configured |
| --- | --- | --- |
| AT&T | Careers site serves a bot challenge or HTTP 403 | Freehire's name search returns AT&T subsidiaries, a conference center and authorized retailers, but no canonical AT&T company record |
| HCA Healthcare | Careers site answers HTTP 403 | Freehire's `hca-healthcare` record is HCA Healthcare UK; attaching its London hospital postings to the US company would be wrong |
| DENSO Robotics | The main DENSO careers site answers HTTP 403 | DENSO North America is already collected by `denso-oracle`; Freehire does not separate robotics from the parent company, so a second feed would duplicate the same employer |

## 3. Reachable job system, but no adapter for it yet

| Job system | Priority companies | Others |
| --- | --- | --- |
| SuccessFactors tile layout (no result total, so completeness cannot be judged) | KUKA, CNH Industrial, TE Connectivity | |
| SuccessFactors without a career-site search | BMW Group | American Airlines |
| Kenexa BrassRing | General Atomics | |
| Paycor (listing built by JavaScript) | Vecna Robotics, Neocis | |
| Teamtailor | Starship Technologies (1 posting) | |
| Gem | Chef Robotics (1 posting) | |
| Personio | Cartken (offices named without a country) | |
| Factorial | THEKER Robotics | |
| Y Combinator Work at a Startup (no ATS behind it) | Ultra (warehouse robots) | |
| Beesite / Jobylon / Dayforce | Porsche / Einride / Amphenol (a UK unit) | |
| Eightfold with no derivable list endpoint | none | Chevron, Citigroup, American Express, Capital One, Morgan Stanley |

Jibe also serves Arm, PepsiCo and State Farm, which wait for the Fortune 500 pass.

## 4. Board found, but not usable as is

| Company | Problem |
| --- | --- |
| Xwing | The careers page links to a Greenhouse board that answers HTTP 404. |
| Realtime Robotics | freehire lists Workable postings, but the account name behind them could not be identified. |
| Kratos Defense | The only board found, on Paylocity, belongs to "Kratos Industries"; it could not be confirmed as Kratos Defense. |
| TransDigm | Only subsidiary ADP sites (such as `whippany`) were found, not a group board. |
| Hyundai Motor Group | Its SuccessFactors site lists European postings only. |
| JPMorgan Chase | Its Oracle board holds 7,318 postings. A keyword search matches description text, so it cannot narrow the board. Deferred, since JPMorgan isn't a priority. |
| Mitsubishi Electric, Daifuku | Covered by one board each (Mitsubishi Electric US `meus-mea`; Daifuku Intralogistics America). Their second boards, MEPPI (HRM Direct) and Wynright (iCIMS), wait, because the registry keeps one source per company. |
| General Dynamics | Only Electric Boat is covered. GDIT has a public Workday board but is IT services; no board was found for the other units. |

## 5. No public board found (probed 2026-10-14, twice on 2026-10-15, and in freehire's catalog on 2026-10-18)

These companies have no board under any guessed name on the providers the monitor reads, and
freehire's catalog of 294,000 companies has none either, or only aggregator reposts.

- **Robotics:** Aurora Innovation, Covariant, GreyOrange, Unitree, AGIBOT, Pudu Robotics, Geek+,
  Kawasaki Robotics, Sarcos, Vention, Electric Sheep, Monarch Tractor, Hello Robot, Moon Surgical,
  Vicarious Surgical. From the 2026-10-20 list: Booster Robotics, X Square Robot, EngineAI, LimX
  Dynamics, ROBOTERA, Sharpa, CenoBots, August Robotics, AITAN eyesAtop and Mecka (freehire has
  only aggregator reposts of Mecka). The Chinese companies recruit through domestic systems such as
  Moka and Feishu, which the monitor does not read.
- **Aerospace and defense:** Huntington Ingalls, Spirit AeroSystems.
- **Automotive and mobility:** Kia, BYD, Renault, Mazda, Geely, SAIC, Chery, Plus, Luminar, Canoo,
  Dana, AGCO, Volvo Cars.
- **Industrial:** Siemens (only its Healthineers and Energy subsidiaries have boards), Parker
  Hannifin, Ingersoll Rand, Garmin (only a Romanian office's Workable board), Seagate.

## Reachable, but deferred to a later pass

Their public boards answer, but they are less central to robotics and mechanical work, so they wait
for the Fortune 500 pass: Thermo Fisher Scientific, Danaher, Hewlett Packard Enterprise, Leidos,
GDIT (General Dynamics), NXP, Arm, PepsiCo, State Farm.

## Rejected look-alike boards

These board names belong to a different company, so they are not used: `figure` (Figure Lending),
`archer` (a veterinary clinic; Archer Aviation's board is `archer56`), `radiant` (an AI cloud
company, not Radiant Nuclear), `slate` (on Lever, the Slate podcast publisher; on Ashby, an
unrelated video-software company), `sila` (Sila Services, an HVAC company, not Sila
Nanotechnologies), `monogram` (an AI design startup, not Monogram Orthopaedics), `persona` on Ashby
(Persona the identity-verification company; Persona AI's board is `persona.ai`), `emerson` on
Workday (Emerson College), Vention's `join.ventionteams.com` and Teamtailor site (Vention the
software outsourcer, not Vention the robotics company), and the Kia and Mazda boards in freehire's
catalog (car dealerships), `ultra` on Ashby (a nicotine-pouch brand, not Ultra the robotics
company), and `genesis-molecular-ai` on Ashby (a drug-discovery company; Genesis AI's board is
`genesis`). Allen Control Systems also has a Greenhouse board (`allencontrolsystems`), but nothing
on it has changed since March 2026; its Ashby board is the live one.
