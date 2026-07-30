"""
Candidate stock universe, organized by (sector, market-cap tier).

This is the FULL candidate pool from which the actual benchmark universe is
sampled (see select_universe.py). Documenting the whole pool, the sampling
method and the random seed here is deliberate: it lets anyone verify the
final ticker list wasn't hand-picked to make the agent look good. A query
agent can be flattered by an easy universe just as easily as a trading
strategy can — e.g. a universe of only large caps has no thin-volume days,
no wide sector spread, and far fewer of the ties and near-ties that expose
ranking bugs.

This pool is reproduced verbatim from the attached `trading-bot` project
(`data/universe_pool.py`). Reusing an already-published pool rather than
writing a fresh one is the point: the candidate set was fixed before this
project existed, so it cannot have been shaped around this benchmark.

Cap tiers are approximate, based on free-float market cap as of mid-2026:
  LARGE  = Nifty 50 / Nifty Next 50 constituents
  MID    = Nifty Midcap 150 constituents
  SMALL  = Nifty Smallcap 250 constituents

Sector tags are approximate GICS-like buckets, kept coarse on purpose. Two
are known to be imprecise and are kept anyway rather than silently improved,
because changing an inherited label mid-project is how a "disclosed" pool
quietly stops being the disclosed pool: SRF is a chemicals business tagged
Diversified_Financials_Other, and RATNAMANI (steel tubes) is tagged
Auto_AutoAncillary. See README "Known limits".
"""

POOL = {
    # sector: {"LARGE": [...], "MID": [...], "SMALL": [...]}
    "IT_Services": {
        "LARGE": ["TCS", "INFY", "WIPRO", "HCLTECH"],
        "MID": ["COFORGE", "PERSISTENT", "MPHASIS"],
        "SMALL": ["HAPPSTMNDS", "LATENTVIEW", "ZENSARTECH"],
    },
    "Private_Banks": {
        "LARGE": ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK"],
        "MID": ["FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK"],
        "SMALL": ["CUB", "DCBBANK"],
    },
    "PSU_Banks_Financials": {
        "LARGE": ["SBIN"],
        "MID": ["BANKBARODA", "PNB", "CANBK"],
        "SMALL": ["UNIONBANK", "MAHABANK"],
    },
    "NBFC_Insurance": {
        "LARGE": ["BAJFINANCE", "BAJAJFINSV"],
        "MID": ["MFSL", "CHOLAFIN", "LICHSGFIN"],
        "SMALL": ["SPANDANA", "CREDITACC"],
    },
    "Energy_OilGas": {
        "LARGE": ["RELIANCE", "ONGC", "COALINDIA", "NTPC", "POWERGRID"],
        "MID": ["GUJGASLTD", "PETRONET"],
        "SMALL": ["GSPL", "GAIL"],
    },
    "FMCG": {
        "LARGE": ["HINDUNILVR", "ITC", "NESTLEIND"],
        "MID": ["GODREJCP", "MARICO", "COLPAL"],
        "SMALL": ["RADICO", "BAJAJCON"],
    },
    "Auto_AutoAncillary": {
        "LARGE": ["MARUTI", "TATAMOTORS", "M&M"],
        "MID": ["ESCORTS", "BHARATFORG", "BALKRISIND"],
        "SMALL": ["RATNAMANI", "SANDHAR"],
    },
    "Pharma_Healthcare": {
        "LARGE": ["SUNPHARMA", "CIPLA", "DRREDDY"],
        "MID": ["AUROPHARMA", "IPCALAB", "ALKEM"],
        "SMALL": ["JBCHEPHARM", "RAINBOW"],
    },
    "Metals_Mining": {
        "LARGE": ["TATASTEEL", "JSWSTEEL", "HINDALCO"],
        "MID": ["JINDALSTEL", "NMDC"],
        "SMALL": ["RATNAMANI", "KIRLOSENG"],
    },
    "Cement_Building": {
        "LARGE": ["ULTRACEMCO", "GRASIM"],
        "MID": ["SHREECEM", "AMBUJACEM"],
        "SMALL": ["JKPAPER", "CENTURYPLY", "GRINDWELL"],
    },
    "Consumer_Durables_Retail": {
        "LARGE": ["TITAN", "ASIANPAINT"],
        "MID": ["VOLTAS", "TRENT", "PAGEIND"],
        "SMALL": ["SYMPHONY", "VGUARD", "KPRMILL"],
    },
    "Telecom": {
        "LARGE": ["BHARTIARTL"],
        "MID": [],
        "SMALL": ["ROUTE", "TATACOMM"],
    },
    "Capital_Goods_Infra": {
        "LARGE": ["LT"],
        "MID": ["CUMMINSIND", "POLYCAB", "CONCOR"],
        "SMALL": ["TRIVENI", "TEAMLEASE"],
    },
    "Realty": {
        "LARGE": [],
        "MID": ["GODREJPROP", "OBEROIRLTY"],
        "SMALL": ["SOBHA"],
    },
    "Diversified_Financials_Other": {
        "LARGE": ["ADANIENT"],
        "MID": ["SRF", "ABCAPITAL"],
        "SMALL": ["CAMS", "CLEAN", "GALAXYSURF"],
    },
}

# Coarse sector groups, used to populate the `sectors.sector_group` column so
# the schema has a genuine three-table join path (prices -> instruments ->
# sectors) rather than only a two-table one.
SECTOR_GROUP = {
    "IT_Services": "Technology",
    "Private_Banks": "Financials",
    "PSU_Banks_Financials": "Financials",
    "NBFC_Insurance": "Financials",
    "Diversified_Financials_Other": "Financials",
    "Energy_OilGas": "Energy_Utilities",
    "FMCG": "Consumer_Staples",
    "Auto_AutoAncillary": "Consumer_Discretionary",
    "Consumer_Durables_Retail": "Consumer_Discretionary",
    "Pharma_Healthcare": "Healthcare",
    "Metals_Mining": "Materials",
    "Cement_Building": "Materials",
    "Telecom": "Communication",
    "Capital_Goods_Infra": "Industrials",
    "Realty": "Real_Estate",
}

# Registered company names, used to populate `instruments.name`. Only the
# selected universe needs an entry; a missing name is a hard error in
# build_db.py rather than a silent NULL, so the instruments table can never
# ship half-populated.
COMPANY_NAME = {
    "ALKEM": "Alkem Laboratories Ltd",
    "ASIANPAINT": "Asian Paints Ltd",
    "BAJAJCON": "Bajaj Consumer Care Ltd",
    "BAJAJFINSV": "Bajaj Finserv Ltd",
    "BALKRISIND": "Balkrishna Industries Ltd",
    "BHARTIARTL": "Bharti Airtel Ltd",
    "GODREJPROP": "Godrej Properties Ltd",
    "GRASIM": "Grasim Industries Ltd",
    "HDFCBANK": "HDFC Bank Ltd",
    "JBCHEPHARM": "J.B. Chemicals & Pharmaceuticals Ltd",
    "JINDALSTEL": "Jindal Steel & Power Ltd",
    "JKPAPER": "JK Paper Ltd",
    "LATENTVIEW": "Latent View Analytics Ltd",
    "LICHSGFIN": "LIC Housing Finance Ltd",
    "MARICO": "Marico Ltd",
    "NESTLEIND": "Nestle India Ltd",
    "PERSISTENT": "Persistent Systems Ltd",
    "POWERGRID": "Power Grid Corporation of India Ltd",
    "RATNAMANI": "Ratnamani Metals & Tubes Ltd",
    "SHREECEM": "Shree Cement Ltd",
    "SOBHA": "Sobha Ltd",
    "SPANDANA": "Spandana Sphoorty Financial Ltd",
    "SRF": "SRF Ltd",
    "SUNPHARMA": "Sun Pharmaceutical Industries Ltd",
    "TATACOMM": "Tata Communications Ltd",
    "TATASTEEL": "Tata Steel Ltd",
    "TCS": "Tata Consultancy Services Ltd",
    "TRIVENI": "Triveni Engineering & Industries Ltd",
}

if __name__ == "__main__":
    total = sum(len(v) for sector in POOL.values() for v in sector.values())
    uniq = len({t for sector in POOL.values() for v in sector.values() for t in v})
    print(f"Sectors: {len(POOL)}, pool entries: {total}, unique tickers: {uniq}")
