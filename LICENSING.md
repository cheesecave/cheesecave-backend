# KohakuHub Licensing Structure

*Last Updated: January 2025*

KohakuHub uses a **dual-licensing strategy** to balance open-source principles with sustainable development.

---

## Overview

| Component | License | Location | Commercial Use |
|-----------|---------|----------|----------------|
| **KohakuHub Core** | AGPL-3.0 | Root of repository | ✅ Unrestricted |
| **Dataset Viewer Backend** | Kohaku Software License 1.0 | `src/kohakuhub/datasetviewer/` | ⚠️ Trial limits ($25k/3mo) |
| **Dataset Viewer Frontend** | Kohaku Software License 1.0 | `src/kohaku-hub-ui/src/components/DatasetViewer/` | ⚠️ Trial limits ($25k/3mo) |
| **Dataset Viewer Integration** | AGPL-3.0 | (Pages that use DatasetViewer) | ✅ Unrestricted |

---

## License Details

### 1. KohakuHub Core (AGPL-3.0)

**Applies to:** All code except explicitly marked Dataset Viewer components

**Key Terms:**
- ✅ Free for commercial and non-commercial use
- ✅ Can modify and redistribute
- ⚠️ Must release source code if modified
- ⚠️ Must release source code if used as network service (AGPL requirement)
- ✅ No revenue restrictions

**Files Included:**
- All Python backend code (except `src/kohakuhub/datasetviewer/`)
- All frontend code (except `src/kohaku-hub-ui/src/components/DatasetViewer/`)
- Database models, API routes, auth system, etc.

**Why AGPL-3?**
- Ensures contributions remain open-source
- Prevents proprietary forks from hiding improvements
- Compatible with most open-source ecosystems

---

### 2. Dataset Viewer (Kohaku Software License 1.0)

**Applies to:**
- `src/kohakuhub/datasetviewer/` (Backend API)
- `src/kohaku-hub-ui/src/components/DatasetViewer/` (Frontend components)

**Key Terms:**
- ✅ Free for **non-commercial** use (personal, academic, research)
- ⚠️ **Commercial trial**: 3 months OR $25,000 revenue/year (whichever comes first)
- ⚠️ After trial limits, requires paid commercial license
- ✅ Must release source code (like AGPL)
- ✅ Patent grant included
- ❌ Prohibited uses: military, surveillance, biometric tracking

**Commercial Trial Details:**
- You can use Dataset Viewer commercially for **up to 3 months** for free
- OR if your revenue stays below $25,000/year, you can use it indefinitely during those 3 months
- After either threshold is exceeded, contact `kohaku@kblueleaf.net` for commercial license

**Why Kohaku License?**
- Allows experimentation and small-scale commercial use
- Ensures sustainable development through commercial licensing
- Prevents large companies from exploiting free work

**Source Code Requirements:**
- You **MUST** make source code available (like AGPL)
- Derivative works **MUST** use Kohaku Software License
- Cannot create proprietary closed-source versions

---

## Third-Party Component: LakeFS

KohakuHub stores repository history in [LakeFS](https://lakefs.io), which runs
as a separate service and keeps its own license.

- **Up to 1.86.0:** LakeFS is licensed under Apache 2.0. The Docker bundle pins
  `treeverse/lakefs:1.86.0`.
- **1.87.0 and later:** LakeFS is licensed under the Business Source License
  1.1. Production use is limited to the unmodified release and to your
  organization's internal use. Each release becomes Apache 2.0 four years after
  its publication.

Assess those terms before upgrading LakeFS past 1.86.0, especially for a hub
open to outside users. See [LakeFS Compatibility](docs/deployment/lakefs.md).

---

## Integration Strategy

### How Dataset Viewer Integrates with KohakuHub

**Dependency Direction:** KohakuHub Core (AGPL-3) → Dataset Viewer (Kohaku License)

**Key Point:** KohakuHub main part depends on Dataset Viewer as an optional dependency, not the other way around. Therefore:

1. **KohakuHub remains AGPL-3** - The main project's license is NOT affected by its optional dependencies
2. **Dataset Viewer uses Kohaku License** - The module itself has commercial restrictions
3. **Dataset Viewer is optional** - Can be removed without breaking KohakuHub

### License Application by Use Case

| Use Case | Applies To | License Required |
|----------|-----------|------------------|
| **Use KohakuHub WITH Dataset Viewer** | Both components | AGPL-3 + Kohaku License |
| **Use KohakuHub WITHOUT Dataset Viewer** | KohakuHub only | AGPL-3 only |
| **Modify Dataset Viewer code** | Dataset Viewer | Kohaku License (derivative work) |
| **Fork KohakuHub (keep Dataset Viewer)** | Both components | AGPL-3 + Kohaku License |
| **Fork KohakuHub (remove Dataset Viewer)** | KohakuHub only | AGPL-3 only |

**Important:** KohakuHub's AGPL-3 license is NOT affected by Dataset Viewer's Kohaku License because it's an optional dependency, not a core component

---

## Commercial Usage Scenarios

### Scenario 1: Personal Blog / Academic Research

**License:** Both AGPL-3 and Kohaku License allow free use

**Requirements:**
- None! Use freely.

### Scenario 2: Small Startup (Revenue < $25k/year)

**License:** Kohaku License trial period (3 months)

**Requirements:**
- Can use Dataset Viewer for **3 months** free
- After 3 months, must either:
  - Stop using Dataset Viewer, OR
  - Purchase commercial license

**Cost:** Contact for pricing

### Scenario 3: Medium Business (Revenue $25k-$100k/year)

**License:** Kohaku License requires commercial license immediately

**Requirements:**
- Must purchase commercial license before deployment

**Cost:** Contact for pricing (likely revenue-sharing or flat fee)

### Scenario 4: Large Enterprise (Revenue > $100k/year)

**License:** Kohaku License requires commercial license immediately

**Requirements:**
- Must purchase commercial license
- Likely involves custom terms

**Cost:** Contact for enterprise pricing

### Scenario 5: Using KohakuHub Without Dataset Viewer

**License:** AGPL-3 only

**Requirements:**
- Must release source code if modified
- Must release source code if run as network service
- No revenue restrictions

**How to Remove Dataset Viewer:**
1. Delete `src/kohakuhub/datasetviewer/` folder
2. Delete `src/kohaku-hub-ui/src/components/DatasetViewer/` folder
3. Remove Dataset Viewer imports from `main.py` and pages
4. Run tests to ensure nothing breaks

---

## FAQ

### Q: Can I use KohakuHub commercially?

**A:** Yes! The core is AGPL-3, which allows commercial use. You only need a commercial license if you use the **Dataset Viewer** feature beyond the trial limits.

### Q: Can I remove Dataset Viewer and just use KohakuHub?

**A:** Yes! Dataset Viewer is an optional module. Remove it and you're 100% AGPL-3.

### Q: If I modify Dataset Viewer, what license applies?

**A:** Your modifications must be released under Kohaku Software License 1.0. You **cannot** make proprietary closed-source versions.

### Q: Can I integrate Dataset Viewer into my own product?

**A:** Yes, but:
- For non-commercial use: Free
- For commercial use: 3-month trial OR $25k revenue limit, then requires commercial license
- Your product must include Dataset Viewer source code (copyleft requirement)

### Q: What if my company revenue is $1M/year but Dataset Viewer only generates $10k?

**A:** According to the license, "Total Revenue" includes:
> "For internal business systems: The total revenue of the organization using the Software for business operations"

So yes, you'd need a commercial license.

### Q: Can I host KohakuHub as a service (SaaS)?

**A:** Yes, but:
- AGPL-3 requires you to release your modifications
- Kohaku License requires commercial license if revenue > $25k/year OR duration > 3 months
- You must display attribution notices to end users

### Q: What about the rate limiter and parsers in Dataset Viewer?

**A:** They're part of the Dataset Viewer module, so Kohaku License applies.

### Q: Can I use Dataset Viewer backend but write my own frontend?

**A:** Yes! The backend API (`src/kohakuhub/datasetviewer/`) is Kohaku-licensed, so:
- Your custom frontend can be any license you want (it's not a derivative work)
- But you still need commercial license if usage exceeds trial limits

### Q: Is this open source?

**A:** It depends on your definition:
- **AGPL-3 core:** Yes, OSI-approved open source
- **Dataset Viewer (Kohaku License):** "Source available" but with commercial restrictions

The Kohaku License is **NOT** OSI-approved open source due to commercial restrictions, but it's more open than typical proprietary licenses (source code required, patent grant, etc).

---

## How to Get a Commercial License

### For Dataset Viewer

**Contact:** kohaku@kblueleaf.net

**Information to Include:**
1. Company name and size
2. Expected revenue from services using Dataset Viewer
3. Use case description
4. Expected number of users/deployments

**Pricing Models:**
- Flat annual fee
- Revenue sharing (X% of revenue attributable to Dataset Viewer)
- Per-seat pricing
- Custom enterprise agreements

**Negotiable Terms:**
- Source code access to future updates
- Priority support
- Custom features
- SLA guarantees

---

## Summary Table

| Use Case | Core License | Dataset Viewer License | Action Required |
|----------|--------------|------------------------|-----------------|
| Personal use | ✅ Free (AGPL-3) | ✅ Free (Kohaku) | None |
| Academic research | ✅ Free (AGPL-3) | ✅ Free (Kohaku) | None |
| Startup (<3 months) | ✅ Free (AGPL-3) | ✅ Trial (Kohaku) | None (during trial) |
| Startup (>3 months) | ✅ Free (AGPL-3) | ⚠️ License required | Get commercial license |
| Business (>$25k/year) | ✅ Free (AGPL-3) | ⚠️ License required | Get commercial license |
| Remove Dataset Viewer | ✅ Free (AGPL-3) | N/A (not using it) | None |
| Fork without Dataset Viewer | ✅ Free (AGPL-3) | N/A | Must keep AGPL-3 |

---

## Compliance Checklist

### If Using KohakuHub Core Only (No Dataset Viewer)

- [ ] Include AGPL-3 license text
- [ ] Provide source code to users (if modified or deployed as service)
- [ ] Display "Powered by KohakuHub" attribution (recommended but not required)

### If Using Dataset Viewer (Within Trial Limits)

- [ ] Include both AGPL-3 and Kohaku License 1.0 texts
- [ ] Provide source code to users (required by both licenses)
- [ ] Display attribution: "Dataset Viewer licensed under Kohaku Software License by KohakuBlueLeaf"
- [ ] Track usage duration and revenue (to know when trial ends)

### If Using Dataset Viewer (Commercial License Obtained)

- [ ] Include license texts
- [ ] Provide source code to users
- [ ] Display attribution notices
- [ ] Comply with commercial license terms
- [ ] Pay license fees as agreed

---

## Contribution Guidelines

### Contributing to KohakuHub Core (AGPL-3)

Your contributions will be licensed under AGPL-3. By submitting a PR, you agree to this.

### Contributing to Dataset Viewer (Kohaku License)

Your contributions will be licensed under Kohaku Software License 1.0. By submitting a PR to files in:
- `src/kohakuhub/datasetviewer/`
- `src/kohaku-hub-ui/src/components/DatasetViewer/`

You agree to license your contributions under Kohaku Software License 1.0.

**Note:** Contributors do not lose rights to their contributions - they retain all rights while granting KohakuBlueLeaf the right to distribute under Kohaku License.

---

## Questions?

**General inquiries:** Open an issue on GitHub
**Commercial licensing:** kohaku@kblueleaf.net
**Legal questions:** Consult your legal team or contact kohaku@kblueleaf.net

---

**Disclaimer:** This document is for informational purposes and is not legal advice. Always consult with a qualified attorney for legal matters.
