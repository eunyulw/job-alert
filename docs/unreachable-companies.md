# Companies the monitor cannot reach

Every company here is wanted but has no working source. Each entry gives the specific reason. A
provider's refusal (a bot challenge, an HTTP 403, or an API that answers "not authorized") is never
worked around; those companies wait for a legitimate route, such as an email alert or a provider
that opens its API. Update this list on every expansion pass: remove a company when a source goes
live, and add every company that was probed and missed.

Robotics, mechanical, aerospace and automotive companies come first, because those are the search's
priority. Last updated 2026-10-15.

## 1. Refused by the provider (not circumvented)

| Company | What the provider answers | Route that could work |
| --- | --- | --- |
| Tesla | Careers API (`cua-api/apps/careers/state`, `/careers/search`) answers HTTP 403 Access Denied (Akamai) | Gmail source `tesla-email-alerts`, waiting on a LinkedIn alert and the keychain app password |
| Lockheed Martin | Eightfold jobs API answers 403 "Not authorized for PCSX" | Email alerts from the careers site |
| Ford Motor Company | Eightfold API 403 "Not authorized for PCSX"; careers site serves a bot challenge | Email alerts |
| John Deere | Eightfold API 403 "Not authorized for PCSX"; careers site serves a bot challenge | Email alerts |
| Eaton | Eightfold API 403 "Not authorized for PCSX" | Email alerts |
| Whirlpool | Eightfold API 403 "Not authorized for PCSX" | Email alerts |
| Lam Research | Eightfold API 403 "Not authorized for PCSX" | Email alerts |
| Qualcomm | Eightfold API 403 "Not authorized for PCSX"; its Workday site lists 0 postings | Email alerts |
| Rivian, Toyota Motor, Gecko Robotics, Daifuku | Careers site serves a bot challenge; no public ATS board found | Email alerts |
| Symbotic, Teradyne, OMRON, DENSO, DENSO Robotics, CMR Surgical, Honda Motor, Mercedes-Benz Group, Ferrari, General Dynamics | Careers site answers HTTP 403. General Dynamics' GDIT unit has a public Workday board, but GDIT is IT services | Email alerts |

Outside the priority areas, these careers sites also serve a bot challenge or answer HTTP 403:
Adobe, Broadcom, Cisco, Cloudflare, Microsoft, Snowflake, Cadence, OpenAI, ServiceNow, Synopsys,
TSMC, Tencent and xAI. Among Fortune 100 companies: AT&T, AbbVie, ADM, CVS Health, Cencora, Cigna,
Freddie Mac, Humana, Lowe's, Merck, Performance Food Group, P&G, Sysco, TJX, UPS, USAA, United
Airlines, Albertsons, BMS, Centene, ConocoPhillips, Eli Lilly, Fannie Mae, HCA, Liberty Mutual,
Marathon Petroleum, Nationwide, Pfizer, Progressive, StoneX, Valero, Verizon and Wells Fargo.

## 2. Reachable ATS, but the monitor has no adapter for it yet

| ATS | Priority companies | Others |
| --- | --- | --- |
| SAP SuccessFactors | Keyence, Epson Robots, Doosan Robotics, ZF, BMW Group, Tata Motors/JLR | SAP, American Airlines, Exxon Mobil, Phillips 66 |
| Oracle Cloud HCM / Taleo | Honeywell, Emerson Electric | JPMorgan Chase, UnitedHealth Group, Enterprise Products |
| iCIMS | none | Arm, PepsiCo, State Farm |
| Eightfold with no derivable list endpoint | none | Chevron, Citigroup, American Express, Capital One, Morgan Stanley |

Writing a SuccessFactors adapter would unlock the most priority companies.

## 3. Board found, but not usable as is

| Company | Problem |
| --- | --- |
| Saronic | Ashby board answers (231 postings), but one record has no title, so every collection is incomplete. Re-check each pass. |
| Intuitive Surgical | Workday tenant `intuitive` exposes only the `irtc_careers` site (30 postings, one research center). The main board was not found. |
| Stellantis | Workday site `External_Career_Site_ID01` lists 25 postings outside the US. The US board was not found. |
| Emerson Electric | The Workday tenant `emerson` belongs to Emerson College. Emerson Electric uses Oracle Cloud (section 2). |
| Sierra Space | The `snc` Workday tenant has a Sierra Space site with 0 postings. |

## 4. No public board found (probed 2026-10-14 and 2026-10-15)

These companies have no board under any guessed name on Greenhouse, Lever, Ashby, SmartRecruiters,
Workday (whose robots.txt lists a tenant's sites) or Eightfold. Most probably use an ATS whose
tenant name could not be guessed, or one without an adapter. Their careers pages render jobs with
JavaScript and publish no feed.

- **Robotics:** Intuitive Surgical (main board), Aurora Innovation, Applied Intuition, Covariant,
  RightHand Robotics, GreyOrange, Agile Robots, NEURA Robotics, Engineered Arts, Unitree, AGIBOT,
  Pudu Robotics, Geek+, KUKA, FANUC, Yaskawa, Universal Robots, Kawasaki Robotics, Mitsubishi
  Electric, Dusty Robotics, Built Robotics, Sarcos, Viam, Vention, Realtime Robotics, Miso
  Robotics, Chef Robotics, Simbe, Brain Corp, Plus One Robotics, Berkshire Grey, Vecna Robotics,
  Electric Sheep, Monarch Tractor, Burro, Exotec, Bear Robotics, Robust AI, Persona AI, Mytra,
  Hello Robot, Machina Labs, Hadrian, Cartken, Starship, iRobot, SharkNinja, Moon Surgical,
  Vicarious Surgical, PROCEPT BioRobotics, Neocis, Distalmotion.
- **Aerospace and defense:** Joby Aviation, Archer Aviation, BETA Technologies, Wisk, Reliable
  Robotics, Xwing, Saildrone, Axiom Space, Stoke Space, Impulse Space, Firefly Aerospace, ABL,
  Boom Supersonic, Apex, K2 Space, True Anomaly, Castelion, Neros, Overland AI, Forterra, Blue
  Water Autonomy, General Atomics, ULA, Textron, BAE Systems, L3Harris, Huntington Ingalls, Howmet,
  Spirit AeroSystems, HEICO, TransDigm, Kratos, AeroVironment.
- **Automotive and mobility:** Hyundai Motor Group, Kia, Nissan, Subaru, Porsche, Volvo Cars,
  Volkswagen Group, BYD, Aisin, Renault, Mazda, Geely, SAIC, Chery, Gatik, Plus, Einride, Luminar,
  Ouster, Canoo, QuantumScape, Our Next Energy, Lear, Dana, Harley-Davidson, Oshkosh, PACCAR,
  Cummins, AGCO, CNH.
- **Industrial:** Siemens, Schneider Electric, Parker Hannifin, Otis, Corning, Ingersoll Rand,
  Timken, Nordson, Hubbell, Amphenol, TE Connectivity, AMETEK, Snap-on, Garmin, Axon, TAE
  Technologies, Zap Energy, X-energy, Boston Scientific, Zimmer Biomet, Western Digital, Seagate,
  Dell Technologies, AMD, Texas Instruments, Microchip.

## Reachable, but deferred to a later pass

Their public Workday boards answer, but they are less central to robotics and mechanical work, so
they wait for the Fortune 500 pass: Thermo Fisher Scientific, Danaher, Hewlett Packard Enterprise,
Leidos, GDIT (General Dynamics), NXP.

## Rejected look-alike boards

These board names belong to a different company, so they are not used: `figure` (Figure Lending),
`archer` (a veterinary clinic), `radiant` (an AI cloud company, not Radiant Nuclear), `slate` (on
Lever, the Slate podcast publisher; on Ashby, an unrelated video-software company), `sila` (Sila
Services, an HVAC company, not Sila Nanotechnologies), `monogram` (an AI design startup, not
Monogram Orthopaedics) and `emerson` on Workday (Emerson College).
