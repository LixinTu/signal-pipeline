"""Offline checks for names.py. The company names are SIMULATED or copied from public Form D filings.

Run: python tests/check_names.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from names import clean_name, search_names, short_name, strip_legal_suffix  # noqa: E402

results = []


def check(name, cond):
    results.append(bool(cond))
    print("PASS" if cond else "FAIL", "-", name)


# 1. join keys
check("legal suffix dropped", clean_name("Jaccard, Inc") == "jaccard")
check("dotted suffix dropped", clean_name("Tribe Capital Firstlook APP-03, L.P.") == "tribe capital firstlook app 03")
check("ampersand read as and", clean_name("fal - Features & Labels, Inc.") == "fal features and labels")
check("dot inside a name kept as a word break", clean_name("iDox.ai Corp.") == "idox ai")
check("case ignored", clean_name("OBSIDIAN PRIME INC") == clean_name("Obsidian Prime"))
check("only a suffix -> empty key", clean_name("Inc.") == "")

# 2. names as filed, minus the suffix
check("comma and suffix removed", strip_legal_suffix("Dine Bidziil, PBC") == "Dine Bidziil")
check("several suffix words removed", strip_legal_suffix("Acme Co., Ltd.") == "Acme")
check("a name that is only a suffix is kept", strip_legal_suffix("Inc.") == "Inc.")

# 3. short forms
check("generic last word dropped", short_name("Harvey AI") == "Harvey")
check(".ai ending dropped", short_name("iDox.ai") == "iDox")
check("too short to use", short_name("Eco Labs") is None)
check("no generic last word", short_name("Features & Labels") is None)
check("one word has no short form", short_name("fal") is None)

# 4. spellings sent to TheirStack
check("full name first, then short form", search_names("BottomHalf Labs, Inc.") == ["BottomHalf Labs", "BottomHalf"])
check("two-part name split", search_names("fal - Features & Labels, Inc.") == ["fal - Features & Labels", "fal", "Features & Labels"])
check("platforms is generic", search_names("Daytona Platforms Inc.") == ["Daytona Platforms", "Daytona"])
check("only one word dropped", search_names("Sai Intelligence Lab, Inc.") == ["Sai Intelligence Lab", "Sai Intelligence"])
check("case-insensitive duplicates dropped",
      search_names("Acme Labs - ACME LABS") == ["Acme Labs - ACME LABS", "Acme Labs", "Acme"])
check("upper-case name kept as filed", search_names("OBSIDIAN PRIME INC") == ["OBSIDIAN PRIME"])

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
