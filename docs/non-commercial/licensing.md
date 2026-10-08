# Non-Commercial Features Licensing

> **CheeseCave status (2026-10-08):** the Dataset Viewer described below (the backend `src/kohakuhub/datasetviewer/` and the frontend `DatasetViewer` components) has been removed from the CheeseCave repositories. This document is kept as the original upstream text for provenance; its Dataset Viewer sections describe the upstream project and earlier builds only. Everything in the CheeseCave repositories is under AGPL-3.0; see [NOTICE.md](NOTICE.md).

**Understanding the dual-license structure of KohakuHub**

---

## Overview

KohakuHub uses a **dual-license strategy** to balance open-source principles with sustainable development:

1. **KohakuHub Core** → AGPL-3.0 (Fully open source)
2. **Dataset Viewer** → Kohaku Software License 1.0 (Source-available with commercial trial)
3. **KohakuBoard** (Planned) → Kohaku Software License 1.0 (Source-available with commercial trial)

---

## License Breakdown

### KohakuHub Core (AGPL-3.0)

**What it includes:**
- Repository management (models, datasets, spaces)
- Git LFS protocol
- Authentication & authorization
- Organizations & collaboration
- Quota management
- Fallback sources
- API endpoints
- CLI tools

**Terms:**
- ✅ **Free for commercial use** (no restrictions!)
- ✅ Modify and redistribute freely
- ⚠️ **Must release source code** if modified
- ⚠️ **Must release source code** if run as network service (AGPL requirement)
- ✅ No revenue limits
- ✅ No trial periods

**License file:** Root `LICENSE` file (AGPL-3.0)

---

### Dataset Viewer (Kohaku Software License 1.0)

**What it includes:**
- Backend: `src/kohakuhub/datasetviewer/`
- Frontend: `src/kohaku-hub-ui/src/components/DatasetViewer/`
- Dataset preview with streaming
- SQL query support (DuckDB)
- TAR/WebDataset support

**Terms:**
- ✅ **Free for non-commercial use** (personal, academic, research)
- ⚠️ **Commercial trial:** 3 months OR $25,000 revenue/year (whichever comes first)
- ⚠️ **After trial:** Requires paid commercial license
- ✅ **Source code must be available** (like AGPL)
- ✅ Derivative works must use same license
- ❌ **Cannot create closed-source versions**

**License files:**
- `src/kohakuhub/datasetviewer/LICENSE`
- `src/kohaku-hub-ui/src/components/DatasetViewer/LICENSE`

---

### KohakuBoard (Planned - Kohaku Software License 1.0)

**What it will include:**
- Experiment tracking system (WandB replacement)
- Metric logging and visualization
- Run comparison
- Artifact storage
- Python client library

**Terms:** Same as Dataset Viewer (Kohaku Software License 1.0)

**Status:** Not yet implemented (design phase)

---

## Dependency Model

### KohakuHub → Dataset Viewer (Optional Dependency)

```
KohakuHub Core (AGPL-3)
    ↓ (optionally depends on)
Dataset Viewer (Kohaku License)
```

**Key point:** KohakuHub **depends on** Dataset Viewer as an optional dependency, NOT the other way around.

**Why this matters:**
- KohakuHub's AGPL-3 license is **NOT affected** by Dataset Viewer's Kohaku License
- Dataset Viewer is **optional** - can be removed
- Dependency is one-way (KohakuHub → Dataset Viewer)

**Copyright owner (KohakuBlueLeaf) can:**
- ✅ Integrate both licenses in same codebase
- ✅ Choose which license applies to which code
- ✅ Make Dataset Viewer optional or mandatory

**Third-party users must:**
- ⚠️ Comply with BOTH licenses if using both components
- ✅ OR remove Dataset Viewer and use AGPL-3 only

---

## Use Cases & License Requirements

### Case 1: Personal / Academic / Non-Profit

**Components:** KohakuHub + Dataset Viewer

**License compliance:**
- ✅ Free to use both
- ✅ Must release source code if modified (both licenses require this)
- ✅ No commercial trial limits (non-commercial use)

**Attribution required:**
- Display Kohaku License notice (already in UI)

---

### Case 2: Small Commercial Startup (Revenue < $25k/year)

**Components:** KohakuHub + Dataset Viewer

**Trial period:** Up to 3 months

**License compliance:**
- ✅ Free for first 3 months
- ✅ Must release source code if modified
- ⚠️ **After 3 months:** Must purchase commercial license OR remove Dataset Viewer

**Cost after trial:**
- Contact kohaku@kblueleaf.net for pricing
- Typical: Annual license or revenue sharing

**Alternative:**
- Remove Dataset Viewer → AGPL-3 only → No trial limits!

---

### Case 3: Medium-Large Commercial (Revenue > $25k/year)

**Components:** KohakuHub + Dataset Viewer

**License compliance:**
- ⚠️ **Requires commercial license immediately** (no trial)
- ✅ Must release source code if modified

**Cost:**
- Contact kohaku@kblueleaf.net
- Pricing based on company size and use case

**Alternative:**
- Remove Dataset Viewer → AGPL-3 only → Free commercial use!

---

### Case 4: Commercial Without Dataset Viewer

**Components:** KohakuHub only (AGPL-3)

**License compliance:**
- ✅ **Free for commercial use** (no restrictions!)
- ✅ Modify and redistribute freely
- ⚠️ Must release source code if modified/deployed as service

**How to build:**
```bash
# Backend
KOHAKU_HUB_DISABLE_DATASET_VIEWER=true uvicorn kohakuhub.main:app

# Frontend
npm run build:agpl-only
```

**Result:** Pure AGPL-3 build, simpler licensing, no trial limits

---

## Comparison Table

| Scenario | License(s) | Commercial Use | Source Code | Trial Period |
|----------|-----------|----------------|-------------|--------------|
| **Personal/Academic** | AGPL-3 + Kohaku | ✅ Free | ✅ Required | N/A |
| **Startup (<$25k)** | AGPL-3 + Kohaku | ⚠️ 3 months trial | ✅ Required | 3 months |
| **Business (>$25k)** | AGPL-3 + Kohaku | ❌ License required | ✅ Required | None |
| **Commercial (no viewer)** | AGPL-3 only | ✅ Free unlimited | ✅ Required | N/A |
| **Fork (keep viewer)** | AGPL-3 + Kohaku | ⚠️ Trial applies | ✅ Required | 3 months OR $25k |
| **Fork (remove viewer)** | AGPL-3 only | ✅ Free unlimited | ✅ Required | N/A |

---

## Kohaku Software License 1.0 - Key Terms

### What You CAN Do

✅ **Use for free (non-commercial)**
- Personal projects
- Academic research
- Non-profit organizations

✅ **Commercial trial**
- Up to 3 months free commercial use
- OR up to $25,000 revenue/year (whichever comes first)

✅ **Modify and redistribute**
- Create derivative works
- Distribute modified versions
- ⚠️ Must use same license for derivatives

✅ **View source code**
- All source code available
- No obfuscation or hidden code

### What You CANNOT Do

❌ **Commercial use beyond trial without license**
- After 3 months OR >$25k revenue → Must purchase license

❌ **Create closed-source proprietary versions**
- Must release source code (like AGPL)
- Derivative works must use Kohaku License

❌ **Remove copyright notices**
- Attribution required

❌ **Prohibited uses**
- Military/weapons development
- Surveillance systems
- Biometric tracking
- Malware

### What You MUST Do

✅ **Provide source code**
- When distributing or modifying
- Include complete build instructions
- Document modifications

✅ **Include license**
- Copy of Kohaku Software License with distribution
- Attribution notice in UI (for web services)

✅ **Comply with trial limits**
- Track usage duration
- Track revenue attributable to the software
- Purchase license when limits exceeded

---

## Revenue Attribution

**How to calculate "revenue attributable to the Software":**

### For SaaS/Service Providers

**Total revenue from services using Dataset Viewer**

Example:
- Your platform charges $10/user/month
- 100 users use your platform
- Platform includes Dataset Viewer feature
- **Attributable revenue:** $1,000/month = $12,000/year
- **Status:** Under $25k limit ✅ (but check 3-month limit!)

### For Product Vendors

**Total revenue from products incorporating Dataset Viewer**

Example:
- You sell analytics software for $500/license
- Software includes Dataset Viewer component
- 100 licenses sold
- **Attributable revenue:** $50,000/year
- **Status:** Exceeds $25k limit ❌ → License required

### For Internal Business Systems

**Total revenue of organization using the Software**

Example:
- Your company revenue: $5M/year
- Uses Dataset Viewer internally for data analysis
- **Attributable revenue:** $5M/year
- **Status:** Exceeds $25k limit ❌ → License required

**Important:** For internal use, the **company's total revenue** counts, not just revenue from the specific feature.

---

## Getting a Commercial License

### What You Get

✅ **No trial limits** - Use indefinitely
✅ **Commercial rights** - Full commercial use
✅ **Support** (optional tier)
✅ **Updates** (optional tier)
✅ **Legal protection** (indemnification available)

### Pricing Models

**Options (negotiable):**
1. **Flat annual fee** - Simple, predictable ($2k-10k/year typical)
2. **Revenue sharing** - X% of revenue attributable to Dataset Viewer
3. **Per-seat licensing** - $X per user/developer
4. **Custom enterprise agreement** - Tailored to your needs

**Factors affecting price:**
- Company size and revenue
- Number of users/deployments
- Level of support needed
- Custom feature requests

### Contact

**Email:** kohaku@kblueleaf.net

**Include in inquiry:**
1. Company name and size
2. Use case description
3. Expected revenue from Dataset Viewer
4. Number of users/deployments
5. Preferred pricing model

**Response time:** Typically 1-3 business days

---

## Compliance Checklist

### Using KohakuHub WITH Dataset Viewer

**Non-Commercial:**
- [ ] Display attribution notice (auto-included in UI)
- [ ] Provide source code if distributing
- [ ] Include both license files

**Commercial (during trial):**
- [ ] Track start date of commercial use
- [ ] Track revenue attributable to Dataset Viewer
- [ ] Display attribution notice
- [ ] Provide source code if distributing
- [ ] **Set reminder:** Contact for license before trial expires

**Commercial (licensed):**
- [ ] Pay license fees as agreed
- [ ] Display attribution notice
- [ ] Comply with license terms
- [ ] Provide source code if distributing

---

### Using KohakuHub WITHOUT Dataset Viewer

**AGPL-3 only:**
- [ ] Provide source code if modified
- [ ] Provide source code if run as network service
- [ ] Include AGPL-3 license file
- [ ] No attribution required (but appreciated!)

**Build commands:**
```bash
# Backend
KOHAKU_HUB_DISABLE_DATASET_VIEWER=true uvicorn kohakuhub.main:app

# Frontend
npm run build:agpl-only
```

---

## FAQ

### Q: I'm a small business. Can I use Dataset Viewer?

**A:** Yes! You get a 3-month trial OR until you reach $25k revenue/year. After that, you need a commercial license or can remove Dataset Viewer.

### Q: What if I exceed $25k in month 2?

**A:** The trial ends. You must either:
1. Purchase commercial license, OR
2. Remove Dataset Viewer (rebuild without it)

### Q: Can I switch to AGPL-3 only after trial?

**A:** Yes! Just remove Dataset Viewer components and rebuild. No commercial license needed for core KohakuHub.

### Q: What happens if I don't comply?

**A:** License automatically terminates. You must:
- Stop using Dataset Viewer
- Destroy all copies
- Cease providing services that use it

### Q: Can I negotiate the trial terms?

**A:** No, trial terms are fixed. But you can negotiate commercial license terms (pricing, duration, etc.).

### Q: Does the trial reset if I rebuild?

**A:** No. Trial is based on first commercial use, not per build.

### Q: Can I use for development/testing commercially?

**A:** Yes, development/testing counts toward the 3-month trial period.

### Q: What about open-source contributions?

**A:** Contributing to KohakuHub (AGPL-3) → AGPL-3
Contributing to Dataset Viewer → Kohaku License

You retain rights to your contributions.

---

## Enforcement

### How is compliance verified?

**Self-reporting system:**
- No DRM or phone-home features
- Users responsible for tracking usage
- License operates on good faith + legal enforceability

**If violation suspected:**
1. Email notification (cease & desist)
2. Opportunity to come into compliance (purchase license)
3. Legal action (if necessary)

**Goal:** Compliance, not litigation. Most cases resolved amicably.

---

## Best Practices

### For Startups

1. **Start with trial** - Test Dataset Viewer in production
2. **Track metrics** - Monitor usage duration and revenue
3. **Plan ahead** - Budget for commercial license before trial ends
4. **Alternative ready** - Have AGPL-3 only build ready as backup

### For Enterprises

1. **Evaluate first** - Determine if Dataset Viewer is needed
2. **Contact early** - Discuss licensing before deployment
3. **Negotiate terms** - Custom agreements available
4. **Legal review** - Have your legal team review both licenses

### For Open Source Projects

1. **AGPL-3 only** - Remove Dataset Viewer to keep it simple
2. **Or accept dual license** - If Dataset Viewer is valuable
3. **Document clearly** - Let users know about dual licensing

---

## Comparison with Other Licenses

| License | Commercial Use | Source Code | Copyleft | Network Service |
|---------|----------------|-------------|----------|-----------------|
| **MIT** | ✅ Free | Optional | ❌ No | Not affected |
| **Apache 2.0** | ✅ Free | Optional | ❌ No | Not affected |
| **GPL-3.0** | ✅ Free | Required | ✅ Yes | Not affected |
| **AGPL-3.0** | ✅ Free | Required | ✅ Yes | **Must release** |
| **Kohaku 1.0** | ⚠️ Trial | Required | ✅ Yes | Must release |

**Kohaku License is like AGPL-3 + commercial trial limits**

---

## Why Dual Licensing?

### For Users

**Benefits:**
- ✅ Core features always free (AGPL-3)
- ✅ Try non-commercial features before committing
- ✅ Source code available for both
- ✅ Clear separation (can choose AGPL-3 only)

**Drawbacks:**
- ⚠️ More complex licensing (two licenses)
- ⚠️ Must track trial period for commercial use
- ⚠️ Potential future license costs

### For KohakuBlueLeaf (Author)

**Benefits:**
- ✅ Sustainable development (commercial licenses fund work)
- ✅ Prevent large companies from exploiting free work
- ✅ Balance open-source principles with monetization
- ✅ More resources for feature development

**Drawbacks:**
- ⚠️ Some users may remove non-commercial features
- ⚠️ More complex to explain and enforce
- ⚠️ May limit adoption in some commercial settings

---

## Historical Context

### Why Not Pure AGPL-3 for Everything?

**AGPL-3 problems:**
- Large companies can use freely (no revenue for author)
- Author must fund development from other sources
- Slower development (less resources)
- Risk of abandonment (burnout without funding)

### Why Not Pure Proprietary?

**Proprietary problems:**
- No source code (vendor lock-in)
- No community contributions
- Higher barrier to adoption
- Against open-source principles

### Dual License = Best of Both

**Combines:**
- Open source core (AGPL-3) for community
- Commercial features (Kohaku License) for sustainability
- Source code available for both (no lock-in)
- Clear path from free trial to paid license

**Similar models:**
- MySQL (GPL + Commercial)
- Qt (LGPL + Commercial)
- GitLab (MIT + Proprietary EE)

**Difference:** Kohaku License is **source-available** (not fully proprietary)

---

## Decision Tree

**Should I use Dataset Viewer?**

```
Are you using KohakuHub commercially?
├─ No (personal/academic)
│  └─ ✅ Use freely (both licenses allow)
│
└─ Yes (commercial)
   ├─ Revenue < $25k/year?
   │  ├─ Yes
   │  │  └─ ✅ Use for 3 months (trial)
   │  │     └─ After 3 months?
   │  │        ├─ Still < $25k → ⚠️ Trial ended, need license
   │  │        └─ Now > $25k → ⚠️ Need license
   │  │
   │  └─ No (revenue > $25k)
   │     └─ ⚠️ Need license immediately
   │
   └─ Don't want commercial license?
      └─ ✅ Remove Dataset Viewer (AGPL-3 only)
         └─ Free forever!
```

---

## Contact & Resources

**Commercial licensing inquiries:**
- Email: kohaku@kblueleaf.net
- Subject: "KohakuHub Commercial License Inquiry"

**Technical questions:**
- GitHub Issues: https://github.com/KohakuBlueleaf/KohakuHub/issues
- Discord: https://discord.gg/xWYrkyvJ2s

**Documentation:**
- Main licensing: `/LICENSING.md`
- Build without viewer: `/BUILD_WITHOUT_DATASET_VIEWER.md`
- Kohaku License text: `src/kohakuhub/datasetviewer/LICENSE`

---

## Summary

**Three licensing options:**

1. **Full KohakuHub (with Dataset Viewer)**
   - Licenses: AGPL-3 + Kohaku License
   - Use: Non-commercial free, commercial trial then license
   - Best for: Exploring all features

2. **AGPL-3 Only (without Dataset Viewer)**
   - License: AGPL-3 only
   - Use: Commercial use freely (no limits!)
   - Best for: Production commercial use

3. **Commercial Licensed (with Dataset Viewer)**
   - Licenses: AGPL-3 + Kohaku Commercial License
   - Use: Full commercial rights
   - Best for: Long-term commercial use needing Dataset Viewer

**Choose what works for your needs!**

---

**Disclaimer:** This document is for informational purposes only and does not constitute legal advice. Consult with a qualified attorney for legal matters.
