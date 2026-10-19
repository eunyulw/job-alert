# Companies the monitor cannot reach

Every company here is wanted but has no working source. Each entry gives the specific reason. A
provider's refusal (a bot challenge, an HTTP 403, or an API that answers "not authorized") is never
worked around; those companies wait for a legitimate route, such as an email alert or a provider
that opens its API. Update this list on every expansion pass: remove a company when a source goes
live, and add every company that was probed and missed.

Robotics, mechanical, aerospace and automotive companies come first, because those are the search's
priority. Last updated 2026-10-18, after the freehire board-catalog pass (see
[collection-verification.md](collection-verification.md)), which moved 70 companies off this list.

A company whose careers site refuses us can still be reachable when its job board is published
somewhere else that answers an ordinary request: Ford, Cummins and DENSO run public Oracle boards;
John Deere a SuccessFactors search; Rivian a Jibe API; Toyota and Symbotic Workday sites; Honda a
Phenom site; Gecko an Ashby board; Daifuku and General Dynamics Electric Boat iCIMS sites. Those are
plain public boards, so using them circumvents nothing. freehire (github.com/strelov1/freehire),
whose catalog located most of them, also reaches refusing sites by spoofing a browser's TLS
fingerprint, egressing through residential proxies, running a headless browser, and paying a hosted
scraping service; none of that is used here.

## 1. Refused by the provider (not circumvented)

| Company | What the provider answers | Route that could work |
| --- | --- | --- |
| Tesla | Careers API (`cua-api/apps/careers/state`, `/careers/search`) answers HTTP 403 Access Denied (Akamai) | Gmail source `tesla-email-alerts`, waiting on a LinkedIn alert and the keychain app password |
| Lockheed Martin, Eaton, Whirlpool, Lam Research, Qualcomm | Eightfold jobs API answers 403 "Not authorized for PCSX". freehire reads Eightfold only through residential proxies, so no alternative board exists | Email alerts from each careers site |
| OMRON, DENSO Robotics, CMR Surgical, Mercedes-Benz Group, Ferrari | Careers site answers HTTP 403, and no other public board was found. OMRON's ADP site "Omron 247" is a service unit and is not used | Email alerts |
| SAP | The SuccessFactors career-site search answers HTTP 403 | Email alerts |

Outside the priority areas, these careers sites also serve a bot challenge or answer HTTP 403:
Adobe, Broadcom, Cisco, Cloudflare, Microsoft, Snowflake, Cadence, OpenAI, ServiceNow, Synopsys,
TSMC, Tencent and xAI. Among Fortune 100 companies: AT&T, AbbVie, ADM, CVS Health, Cencora, Cigna,
Freddie Mac, Humana, Lowe's, Merck, Performance Food Group, P&G, Sysco, TJX, UPS, USAA, United
Airlines, Albertsons, BMS, Centene, ConocoPhillips, Eli Lilly, Fannie Mae, HCA, Liberty Mutual,
Marathon Petroleum, Nationwide, Pfizer, Progressive, StoneX, Valero, Verizon and Wells Fargo.

## 2. Reachable job system, but no adapter for it yet

| Job system | Priority companies | Others |
| --- | --- | --- |
| SuccessFactors tile layout (no result total, so completeness cannot be judged) | KUKA, CNH Industrial, TE Connectivity | |
| SuccessFactors without a career-site search | BMW Group | American Airlines |
| Kenexa BrassRing | General Atomics | |
| Paycor (listing built by JavaScript) | Vecna Robotics, Neocis | |
| Teamtailor | Starship Technologies (1 posting) | |
| Gem | Chef Robotics (1 posting) | |
| Personio | Cartken (offices named without a country) | |
| Beesite / Jobylon / Dayforce | Porsche / Einride / Amphenol (a UK unit) | |
| Eightfold with no derivable list endpoint | none | Chevron, Citigroup, American Express, Capital One, Morgan Stanley |

Jibe also serves Arm, PepsiCo and State Farm, which wait for the Fortune 500 pass.

## 3. Board found, but not usable as is

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

## 4. No public board found (probed 2026-10-14, twice on 2026-10-15, and in freehire's catalog on 2026-10-18)

These companies have no board under any guessed name on the providers the monitor reads, and
freehire's catalog of 294,000 companies has none either, or only aggregator reposts.

- **Robotics:** Aurora Innovation, Covariant, GreyOrange, Unitree, AGIBOT, Pudu Robotics, Geek+,
  Kawasaki Robotics, Sarcos, Vention, Electric Sheep, Monarch Tractor, Hello Robot, Moon Surgical,
  Vicarious Surgical.
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
catalog (car dealerships).
