# Runway match-score pass (2026-10-22)

The user's Runway account (app.joinrunway.io) ranks postings by match score. Its top 1,000 postings
on 2026-10-22, from 100% down to 84%, came from 486 companies. Only the company names were
collected, from the listing rows; no posting was opened. The user trusts Runway's choice of roles,
so every real employer on the list was treated as a target. Names are ordered by how many of the
1,000 postings each held.

| Outcome | Companies | Postings |
| --- | ---: | ---: |
| Already monitored | 86 | 330 |
| Added in this pass, each validated with `audit set-source` | 84 | 159 |
| Registered, still no source | 5 | 16 |
| A non-US unit of a monitored company | 3 | 5 |
| Recruiters and job sites, not employers | 6 | 10 |
| Board found, but not readable yet | 13 | 51 |
| No public board found on a supported system | 289 | 429 |

## Added

CoStar Group, Draper, Gates Corporation, GlobalFoundries, Acuity, First Solar, MAHLE, Relling, Rolls-Royce, STERIS, CesiumAstro, GITAI, Leidos, Mill, onsemi, SPX Technologies, ZOLL Medical Corporation, AtriCure, Inc., Badger Meter, Bedrock Robotics, Bracket Bot, CACI International Inc, CRB, Dodge Industrial, Dorman Products, DuPont, Hendrick Motorsports, James Hardie, Kearfott Corporation, Quantinuum, Reflect Orbital, Rugged Robotics, Vertiv, Webco Industries, AgZen, Allegion US, Base Power Company, BRPH, Crusoe, CycloKinetics, DuBois Chemicals, Inc., Entegris, Furientis, Ginkgo Bioworks, Globus Medical, Graphcore, Heidelberg Materials North America, Heven AeroTech, Hose Master LLC, Human Computer Lab, Humble Robotics, Hyliion, Lila Sciences, Mach9, Mantari, Menasha Corporation, Methode Electronics, Mojave Energy Systems, Inc., Moment Energy, Motorola Solutions, NOX METALS, nVent, Orbital Operations, Orbital Robotics Ltd., Organon, Orion Steel, Philips, Plasmidsaurus, Plug Power, Rainmaker Technology Corporation, Regeneron, Rheo Engineering, Rivet Industries, RoboForce, Salient Motion, SEW-EURODRIVE - USA, Smith+Nephew, Sonoco, Stratolaunch, Swarm Aero, Tenstorrent, Terex Corporation, Watts Water Technologies, WEG.

The robotics startups among them joined the every-internship filters; the rest joined the
expansion filters, which alert on engineering titles.

## Already monitored

RTX, Astranis Space Technologies, L3Harris Technologies, KLA, General Motors, Nissan Motor Corporation, Shield AI, Zipline, Eaton, Symbotic, Rockwell Automation, Waymo, Applied Materials, Johnson & Johnson Innovative Medicine, Muon Space, Anduril, AV, GE, GE Vernova, Mach Industries, Magna International, Rivian, Rocket Lab, Sierra Space, SpaceX, Stryker, Amazon, AMETEK, BorgWarner, Bosch, FieldAI, Honeywell Technologies, Northrop Grumman, Oshkosh Delivery, Schneider Electric, Varda Space Industries, ABB, Aptiv, BAE Systems, Inc., Commonwealth Fusion Systems, Formlabs, Oshkosh Corporation, Otis Elevator Co., Vast, Analog Devices, Carrier, Castelion, Curtiss-Wright Corporation, Hadrian, Harbinger, Howmet Aerospace, Hubbell Incorporated, Johnson Controls, Northrop Grumman Mission Systems, Inc., Relativity Space, SharkNinja, Sierra Nevada Corporation, Skydio, Stanley Black & Decker, Inc., Textron Specialized Vehicles, Ursa Major, WD, Aescape, Agility Robotics, Apptronik, Axiom Space, Boston Scientific, Caterpillar Inc., Corning Incorporated, Emerson, ExxonMobil, Figure, Generac, Hermeus, Joby Aviation, K2 Space Corporation, Lam Research, Nike, Nordson Corporation, Overland AI, Regal Rexnord, Rhoda AI, Sensata Technologies, Snap-on, Teradyne, Torc Robotics.

## Registered, still no source

Meta, Siemens, Siemens Healthineers, The Home Depot, Walmart.

## Not added

**Non-US units of monitored companies:** Trane Italia, PACCAR India, Samsung Electronics France.

**Recruiters and job sites:** mouhamedzz, IntelliPro, VetJobs, Laborup, Students Plus Startups, True Talent Solutions Partners.

**Board found, but not readable yet.** A second pass read each remaining company's careers page:

- **Schaeffler:** SuccessFactors tile layout with no result total.
- **ITT Inc.:** iCIMS board refuses its sitemap (HTTP 403).
- **Brown and Caldwell:** UKG, but its board id is not on the careers page.
- **Keysight Technologies:** iCIMS board refuses its sitemap (HTTP 403).
- **Skyband Systems:** its Ashby board answers HTTP 404 to the posting API.
- **Aquatech:** Paycom, which no adapter reads.
- **CMTA, Inc.:** Dayforce, which no adapter reads.
- **Constellation:** iCIMS board refuses its sitemap (HTTP 403).
- **Gentuity, LLC:** Paylocity, which no adapter reads.
- **Gerdau North America:** SuccessFactors tile layout with no result total.
- **MacLean-Fogg:** Dayforce, which no adapter reads.
- **TPC Packaging Solutions:** Teamtailor, which no adapter reads.
- **York Space Systems:** Paycor, which no adapter reads.

**No public board found.** Each name was probed on Greenhouse, Lever, Ashby and SmartRecruiters
under name-derived tokens, and on Workday through each guessed tenant's `robots.txt`. Boards whose
own name was a different company were rejected (for example "brown" is Brown University, "flex"
and "mercury" are fintech boards, "astra" and "evio" are unrelated startups, and "williams" is
Williams Companies, not Williams International). Bayer's and Infinera's Workday sites answered
with no postings. These companies need a careers-page check by hand, starting with the largest:

Johns Hopkins Applied Physics Laboratory, Eclipse Automation, Hendrickson, MIT Lincoln Laboratory, General Dynamics Mission Systems, MACOM, Cushman & Wakefield, The Toro Company, Westinghouse Electric Company, Intralox, Inversion, NASA Jet Propulsion Laboratory, Ralliant, Sargent & Lundy, The Aerospace Corporation, TTM Technologies, AstroForge, Farnsworth Group, Flex, GM Performance Power Units, MDA Space, Mission Technologies, a division of HII, S&C Electric Company, Thermo King, U.S. Tsubaki Power Transmission, LLC, Werfen, Westinghouse Air Brake Technologies Corporation, Ag Express Electronics Inc, AGCO Corporation, Alliance Automotive Group, ATI, AtkinsRéalis, AUTOKINITON, Barrios Technology, Beyond New Horizons, Brooks Automation, Carnegie Mellon University - School of Computer Science - Language Technologies Institute, Charter Manufacturing, Chromalox, Daimler Truck AG, Daimler Truck North America, Fendt, Freeform, Haag, a Salas O'Brien Company, Honda of America Mfg., Inc., iRhythm Technologies, Inc., JTEKT North America Corporation, Kimley-Horn, KION Group, Lanco Integrated, Merrick & Company, MHI Canada Aerospace, Inc., Monarch Quantum, Nominal, Novelis, Revel, Standard Iron, Techtronic Industries - TTI, Universal Orlando Resort, Urschel, Voyager Technologies, Vylor, WSP, Zendar, 4AG Robotics, A. O. Smith Corporation, Aalo Atomics, ACCRETECH SBS, Inc., Aerosonic, AEVEX, Allnorth Consultants Limited, AM Mechanical, American Hydro, Andrenam, ANDRITZ, Apache Stainless Equipment Corporation and Mepaco, Apex Group Inc., Astra, Astrion, Atec, Inc., Atmus Filtration Technologies, ATS Automation, Inc, Attalon, Inc., Axis Automation, Bastian Solutions, Bayer, BCI Burke Company, Becton, Dickinson and Company, Bernhard, Bethany Children's Health Center, BEUMER Group, Blue Bird Corporation, Booz Allen Hamilton, Brelle, Broan-NuTone, Broetje Automation-USA, Cirrus Logic, CIVCO Medical Solutions, Clarius Mobile Health, Clarke Energy, Clean Harbors, Clera, Colesco Inc, Columbus McKinnon Corporation Ltd. UK, CommScope, Compass Systems & Sales, LLC, Cooper Lighting Solutions, CPC - Colder Products Company, CRABI Robotics, Crane Cryogenics, Critical Systems, Crown Equipment Corporation, Crown Technical Systems, CTDI, Custom Manufacturing & Engineering, Daylight Solutions, Dixon Valve, Doosan, DrinkPAK, DRiV, E Ink Holdings Inc., Emcore LLC, Enedym Inc., EngeniusMicro, enVista, Espey Mfg. & Electronics Corp., EVIO, Exela Pharma Sciences LLC, Exponent, Federal Mfg, Filtration Group, Framatome, G. W. Lisk, Garmin, GE HealthCare, Geophysical Survey Systems, Inc. (GSSI), GeoSpectrum Technologies Inc., Getinge, Gradient, Greenheck, Haag-Streit USA, Haas Automation, Inc., Hamilton Beach Brands Inc., Hazelett Corporation, HBK - Hottinger Brüel & Kjær, HellermannTyton North America, Hewlett Packard Enterprise, HGA, Hidden Level, HighRes, Honda R&D Americas, LLC, Hypertherm Associates, Idaho National Laboratory, IMEG, IMT Precision Inc., INFICON, Infinera, Inspire, Interlune, Intramotev, IPG, IREN, ITW Residential Construction - U.S., Joyson Safety Systems, Kiefer Sage, Kingsbury, Inc.®, KLS Martin Group, Kocher+Beck USA, L.P., Komline, Kraken Robotics, KraussMaffei, Kuraray America, Inc., Lake Air Products, Laminar, LANXESS, Linamar Corporation, Lindsay Corporation, LMI Technologies, Lodestar, LT ZERO Asia Pacific, Lynntech, Inc., Mainstream Engineering Corporation, Mason Controls, Matthews International, Mercury Systems, Metalcraft of Mayville, Inc., MGX Equipment Services, Midmark Corporation, Midrex Technologies, Inc., Milkhouse Candle Company, Milwaukee Tool, MiniMed, Molekule, MORSE Corp, MSA - The Safety Company, Nevada System of Higher Education, Nexteer Automotive, Nikkiso Clean Energy & Industrial Gases, Nomagic, North Atlantic Industries, Nucor Rebar Fabrication, NX Automotive Logistics USA, Inc., NXP Semiconductors, Oatey Company, Oceaneering, OLEDWorks, OPmobility, Optomec, Otter, Pacific Industrial Development Corporation (PIDC), Packaging Corporation of America, Palona AI, Parametric Solutions, Inc., Pascal, PCL Construction, Pelican Energy Consultants, LLC, Peraton, PLP, PM Group, Prairie View A&M University, Price Industries, PROENERGY, Propelis, QinetiQ US, Quartus Engineering, Queue, RAVE Aerospace, Real Alloy, Reotemp Instruments, Resonetics, RH2 Engineering, Ricoh Europe, RidgeTech Automation Inc., Riverpoint Medical, Rivian and Volkswagen Group Technologies, Robson Forensic, Inc., Roto, Safran Defense & Space, Inc., Schnabel Engineering, Scientific Applications & Research Associates (SARA), Inc., Scot Forge, Seasats, Signify, Simplehuman, SinnovaTek | SinnoVita | FirstWave, Smiths Detection, SoundOff Signal, South Western Communications, Southern States LLC, Spacesaver Corporation, SpecSys Inc., Specter Aerospace, SRI, SSOE Group, Stainless Motors, Inc., STERIS Instrument Management Services USA, SV MICROWAVE, Syntron Material Handling, Tecomet, Inc, Teledyne Technologies Incorporated, Teleflex, Tempo Innovations, TeraFlex, Inc, The Boring Company, The Boston Beer Company, The Kinetic Group, Thornton Tomasetti, TPS LLC, Transaera, Inc., Tranter, Triad Technologies, LLC, Triumph, Trividia Health, Inc., TS Conductor, UL Solutions, Vantor, Veeco, VEGA Americas, Inc., Veranex, Vertex Pharmaceuticals, Voxel Innovations, VulcanForms Inc., Waev, WhiteWater, Williams International, Xona, Yanfeng, Young & Franklin Tactair, Zymo Research Corp.
