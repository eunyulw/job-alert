# Companies the monitor cannot reach

Every company here is wanted but has no working source. Each entry gives the specific reason. A
provider's refusal (a bot challenge, an HTTP 403, or an API that answers "not authorized") is never
worked around; those companies wait for a legitimate route, such as an email alert or a provider
that opens its API. Update this list on every expansion pass: remove a company when a source goes
live, and add every company that was probed and missed.

Robotics, mechanical, aerospace and automotive companies come first, because those are the search's
priority. Last updated 2026-10-15, after the pass that added the SuccessFactors, Oracle, Rippling,
Workable, BambooHR, Pinpoint and UKG adapters (49 companies moved off this list).

## 1. Refused by the provider (not circumvented)

| Company | What the provider answers | Route that could work |
| --- | --- | --- |
| Tesla | Careers API (`cua-api/apps/careers/state`, `/careers/search`) answers HTTP 403 Access Denied (Akamai) | Gmail source `tesla-email-alerts`, waiting on a LinkedIn alert and the keychain app password |
| Lockheed Martin, Ford Motor Company, John Deere, Eaton, Whirlpool, Lam Research, Qualcomm | Eightfold jobs API answers 403 "Not authorized for PCSX". Ford's and Deere's careers sites also serve a bot challenge, and Qualcomm's Workday site lists 0 postings | Email alerts from each careers site |
| Rivian, Toyota Motor, Gecko Robotics, Daifuku | Careers site serves a bot challenge; no public ATS board found | Email alerts |
| Symbotic, OMRON, DENSO, DENSO Robotics, CMR Surgical, Honda Motor, Mercedes-Benz Group, Ferrari, General Dynamics | Careers site answers HTTP 403. General Dynamics' GDIT unit has a public Workday board, but GDIT is IT services | Email alerts |
| Cummins, SAP | The SuccessFactors career-site search answers HTTP 403 | Email alerts |

Outside the priority areas, these careers sites also serve a bot challenge or answer HTTP 403:
Adobe, Broadcom, Cisco, Cloudflare, Microsoft, Snowflake, Cadence, OpenAI, ServiceNow, Synopsys,
TSMC, Tencent and xAI. Among Fortune 100 companies: AT&T, AbbVie, ADM, CVS Health, Cencora, Cigna,
Freddie Mac, Humana, Lowe's, Merck, Performance Food Group, P&G, Sysco, TJX, UPS, USAA, United
Airlines, Albertsons, BMS, Centene, ConocoPhillips, Eli Lilly, Fannie Mae, HCA, Liberty Mutual,
Marathon Petroleum, Nationwide, Pfizer, Progressive, StoneX, Valero, Verizon and Wells Fargo.

## 2. Reachable job system, but no adapter for it yet

| Job system | Priority companies | Others |
| --- | --- | --- |
| iCIMS | Joby Aviation, AMD, Schneider Electric | Arm, PepsiCo, State Farm |
| Phenom | BAE Systems, Oshkosh, Zimmer Biomet | |
| Radancy | L3Harris, iRobot, SharkNinja | |
| Kenexa BrassRing | General Atomics, ULA (BAE Systems also links one) | |
| Oracle Taleo | Textron | UnitedHealth Group, Enterprise Products |
| Teamtailor | Starship Technologies | |
| SuccessFactors without a career-site search | BMW Group | American Airlines |
| Eightfold with no derivable list endpoint | none | Chevron, Citigroup, American Express, Capital One, Morgan Stanley |

Next adapters to write, by priority companies unlocked: iCIMS (3), Phenom (3), Radancy (3),
BrassRing (2).

## 3. Board found, but not usable as is

| Company | Problem |
| --- | --- |
| Saronic | Ashby board answers (231 postings), but one record has no title, so every collection is incomplete. Re-check each pass. |
| Intuitive Surgical | Workday tenant `intuitive` exposes only the `irtc_careers` site (30 postings, one research center). The main board was not found. |
| Stellantis | Workday site `External_Career_Site_ID01` lists 25 postings outside the US. The US board was not found. |
| Sierra Space | The `snc` Workday tenant has a Sierra Space site with 0 postings. |
| Dusty Robotics, Xwing | The careers page links to a Greenhouse board that answers HTTP 404. |
| TE Connectivity | The SuccessFactors career site answers, but its search returns no result rows. |
| JPMorgan Chase | Its Oracle board holds 7,318 postings. A keyword search matches description text, so it cannot narrow the board. Deferred, since JPMorgan isn't a priority. |

## 4. No public board found (probed 2026-10-14 and twice on 2026-10-15)

These companies have no board under any guessed name on Greenhouse, Lever, Ashby, SmartRecruiters,
Workday (whose robots.txt lists a tenant's sites), Eightfold, Rippling, Workable or Pinpoint. Their
careers pages show no job-system link that a fetch can see; most render jobs with JavaScript or
link out only after interaction.

- **Robotics:** Intuitive Surgical (main board), Aurora Innovation, Covariant, RightHand Robotics,
  GreyOrange, NEURA Robotics, Engineered Arts, Unitree, AGIBOT, Pudu Robotics, Geek+, KUKA, FANUC,
  Kawasaki Robotics, Mitsubishi Electric, Built Robotics, Sarcos, Vention, Realtime Robotics, Miso
  Robotics, Chef Robotics, Simbe, Berkshire Grey, Vecna Robotics, Electric Sheep, Monarch Tractor,
  Burro, Exotec, Bear Robotics, Persona AI, Hello Robot, Machina Labs, Hadrian, Cartken, Moon
  Surgical, Vicarious Surgical, PROCEPT BioRobotics, Neocis.
- **Aerospace and defense:** Archer Aviation, BETA Technologies, Reliable Robotics, Stoke Space,
  Firefly Aerospace, K2 Space, Castelion, Huntington Ingalls, Spirit AeroSystems, HEICO, TransDigm,
  Kratos.
- **Automotive and mobility:** Hyundai Motor Group, Kia, Nissan, Porsche, Volvo Cars, Volkswagen
  Group, BYD, Aisin, Renault, Mazda, Geely, SAIC, Chery, Gatik, Plus, Einride, Luminar, Ouster,
  Canoo, QuantumScape, Dana, Harley-Davidson, AGCO, CNH.
- **Industrial:** Siemens, Parker Hannifin, Otis, Corning, Ingersoll Rand, Nordson, Hubbell,
  Amphenol, AMETEK, Snap-on, Garmin, TAE Technologies, X-energy, Western Digital, Seagate, Dell
  Technologies, Texas Instruments, Microchip.

## Reachable, but deferred to a later pass

Their public Workday boards answer, but they are less central to robotics and mechanical work, so
they wait for the Fortune 500 pass: Thermo Fisher Scientific, Danaher, Hewlett Packard Enterprise,
Leidos, GDIT (General Dynamics), NXP.

## Rejected look-alike boards

These board names belong to a different company, so they are not used: `figure` (Figure Lending),
`archer` (a veterinary clinic), `radiant` (an AI cloud company, not Radiant Nuclear), `slate` (on
Lever, the Slate podcast publisher; on Ashby, an unrelated video-software company), `sila` (Sila
Services, an HVAC company, not Sila Nanotechnologies), `monogram` (an AI design startup, not
Monogram Orthopaedics), `persona` on Ashby (Persona the identity-verification company, not Persona
AI) and `emerson` on Workday (Emerson College).
