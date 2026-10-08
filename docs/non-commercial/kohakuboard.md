# KohakuBoard - Experiment Tracking System

> **CheeseCave status (2026-10-08):** this page describes an upstream component that is not part of CheeseCave. It is kept for provenance only; nothing in the CheeseCave repositories is under the Kohaku Software License.

**WandB replacement with better performance and self-hosting**

**License:** Kohaku Software License 1.0 (Non-Commercial with Trial)
**Status:** Planned (Not Yet Implemented)

---

## Overview

KohakuBoard is a standalone experiment tracking system designed to replace Weights & Biases (WandB) with better performance, self-hosting, and no vendor lock-in. It's optimized for ML/AI training workflows with focus on handling large volumes of metrics efficiently.

### Design Goals

1. **Better than WandB** - Improved smoothing, subsampling, and chart performance
2. **Self-hosted** - No data privacy concerns, no vendor lock-in
3. **High performance** - WebGL charts handle 100K+ points smoothly
4. **Standalone** - Can be deployed independently or integrated with KohakuHub
5. **Open (with limits)** - Source available, commercial trial period

---

## Key Improvements Over WandB

| Feature | WandB | KohakuBoard | Notes |
|---------|-------|-------------|-------|
| **Smoothing** | Simple averaging (poor) | EMA, Gaussian, SMA | Much better quality |
| **Subsampling** | Naive (every Nth point) | LTTB algorithm | Perceptually better |
| **Chart Performance** | Lags with 10K+ points | WebGL handles 100K+ | Plotly.js WebGL mode |
| **Data Privacy** | Cloud-only | Self-hosted | Your data stays yours |
| **Cost** | $50-200/month | Self-hosted | No per-user fees |
| **Integration** | Separate platform | Optional KohakuHub integration | Unified workflow |

---

## Architecture (Planned)

### Standalone Deployment

```
Python Training Script
    ↓ kohaku.log_metrics({"loss": 0.5})
KohakuBoard Backend (/api/board/...)
    ↓ Store in PostgreSQL
Database (metrics table)
    ↓ Query + aggregate
KohakuBoard Frontend
    ↓ Render with Plotly.js (WebGL)
User views interactive charts
```

### Integrated with KohakuHub

```
User trains model
    ↓ kohaku.log_metrics(...)
KohakuBoard stores metrics
    ↓
Auto-link to repository commit
    ↓
Repository "Metrics" tab shows runs
    ↓
Upload model checkpoint to KohakuHub
```

**Benefits:**
- Unified model versioning + experiment tracking
- Link metrics to specific commits
- Store model checkpoints alongside metrics
- Single platform for entire ML workflow

---

## Planned Features

### Core Features (Phase 1)

**Metric Logging:**
- Batch insert (100s of metrics at once)
- Real-time streaming (SSE)
- Multiple run comparison
- Hyperparameter tracking

**Visualization:**
- Line charts (WebGL, 100K+ points)
- Scatter plots
- Histograms
- Heatmaps
- Custom dashboards

**Smoothing Algorithms:**
- EMA (Exponential Moving Average)
- SMA (Simple Moving Average)
- Gaussian smoothing
- Savitzky-Golay filter

**Subsampling:**
- LTTB (Largest Triangle Three Buckets)
- Min-Max-Avg downsampling
- Time-based bucketing

### Advanced Features (Phase 2)

**Collaboration:**
- Project sharing (private/public)
- Team workspaces
- Comments on runs
- Run comparisons

**Analysis:**
- Statistical summaries
- Hyperparameter search visualization
- Parallel coordinates plot
- Run filtering and search

**Artifacts:**
- Model checkpoint storage (S3)
- Config file versioning
- Plot images
- Custom artifacts

### Integration Features (Phase 3)

**KohakuHub Integration:**
- Auto-create project from repository
- Link runs to commits
- Upload checkpoints to repository
- Unified navigation

**ML Framework Support:**
- PyTorch/PyTorch Lightning callbacks
- TensorFlow/Keras callbacks
- JAX integration
- Hugging Face Transformers integration

---

## Database Schema (Planned)

### Core Tables

**projects**
```sql
CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    owner_id INTEGER REFERENCES users(id),
    name VARCHAR(255),
    description TEXT,
    private BOOLEAN DEFAULT true,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    -- Optional: Link to KohakuHub repository
    repository_id INTEGER REFERENCES repositories(id)
);
```

**runs**
```sql
CREATE TABLE runs (
    id SERIAL PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    name VARCHAR(255),
    description TEXT,
    config JSONB,  -- Hyperparameters
    state VARCHAR(50),  -- running, completed, failed, stopped
    created_at TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    hostname VARCHAR(255),
    python_version VARCHAR(50),
    git_commit VARCHAR(255),
    -- Optional: Link to KohakuHub commit
    repository_commit_id VARCHAR(255)
);
```

**metrics**
```sql
CREATE TABLE metrics (
    id BIGSERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    name VARCHAR(255),  -- "train/loss", "val/accuracy"
    value DOUBLE PRECISION,
    step INTEGER,
    timestamp TIMESTAMP,
    UNIQUE(run_id, name, step)
);

-- Critical index for performance
CREATE INDEX idx_metrics_run_name_step ON metrics(run_id, name, step);
```

**artifacts**
```sql
CREATE TABLE artifacts (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    name VARCHAR(255),
    path VARCHAR(500),  -- S3 path
    size BIGINT,
    artifact_type VARCHAR(50),  -- model, config, plot, other
    checksum VARCHAR(64),
    created_at TIMESTAMP
);
```

---

## API Design (Planned)

### Projects

```bash
POST   /api/board/projects              # Create project
GET    /api/board/projects              # List user projects
GET    /api/board/projects/{id}         # Get project
PUT    /api/board/projects/{id}         # Update project
DELETE /api/board/projects/{id}         # Delete project
```

### Runs

```bash
POST   /api/board/projects/{id}/runs    # Create run
GET    /api/board/projects/{id}/runs    # List runs
GET    /api/board/runs/{id}             # Get run details
PUT    /api/board/runs/{id}             # Update run
POST   /api/board/runs/{id}/complete    # Mark complete
POST   /api/board/runs/{id}/stop        # Stop run
DELETE /api/board/runs/{id}             # Delete run
```

### Metrics

```bash
POST   /api/board/runs/{id}/metrics           # Log metrics (batch)
GET    /api/board/runs/{id}/metrics           # Get all metrics
GET    /api/board/runs/{id}/metrics/{name}    # Get specific metric
GET    /api/board/runs/{id}/metrics/stream    # Real-time SSE stream
```

### Comparison

```bash
POST   /api/board/compare                     # Compare multiple runs
GET    /api/board/projects/{id}/compare       # Compare all runs
```

---

## Python Client Library (Planned)

### Basic Usage

```python
import kohaku

# Initialize
kohaku.init(
    project="my-model-training",
    config={
        "learning_rate": 0.001,
        "batch_size": 32,
        "model": "bert-base"
    }
)

# Start a run
with kohaku.run(name="experiment-001") as run:
    # Training loop
    for epoch in range(10):
        for step, batch in enumerate(dataloader):
            loss = train_step(batch)

            # Log metrics
            run.log({
                "train/loss": loss,
                "train/lr": optimizer.lr,
                "epoch": epoch
            }, step=step)

        # Validation
        val_loss, val_acc = validate()
        run.log({
            "val/loss": val_loss,
            "val/accuracy": val_acc
        }, step=step)

        # Save checkpoint
        torch.save(model.state_dict(), f"checkpoint-{epoch}.pt")
        run.log_artifact(f"checkpoint-{epoch}.pt", type="model")

    # Run automatically marked as completed on exit
```

### PyTorch Lightning Integration

```python
from pytorch_lightning.callbacks import Callback

class KohakuLogger(Callback):
    def __init__(self, run):
        self.run = run

    def on_train_batch_end(self, trainer, pl_module, outputs, batch, batch_idx):
        self.run.log({
            "train/loss": outputs['loss'].item()
        }, step=trainer.global_step)

    def on_validation_epoch_end(self, trainer, pl_module):
        metrics = trainer.callback_metrics
        self.run.log({
            "val/loss": metrics['val_loss'].item(),
            "val/acc": metrics['val_acc'].item()
        }, step=trainer.global_step)

# Usage
with kohaku.run(project="my-project") as run:
    trainer = Trainer(callbacks=[KohakuLogger(run)])
    trainer.fit(model)
```

---

## Chart Rendering (Planned)

### Plotly.js WebGL Mode

**Why WebGL:**
- CPU rendering: ~1K points max before lag
- Canvas rendering: ~10K points max
- **WebGL rendering: 100K+ points smoothly** ✅

**Implementation:**
```javascript
import Plotly from 'plotly.js-gl3d-dist-min'

const trace = {
  x: steps,  // [0, 1, 2, ..., 100000]
  y: values,  // [2.5, 2.3, 2.1, ...]
  type: 'scattergl',  // WebGL mode!
  mode: 'lines',
  line: { color: '#3b82f6', width: 2 }
}

Plotly.newPlot('chart', [trace], layout, config)
```

### Smoothing Algorithms

**Exponential Moving Average (EMA):**
```javascript
function smoothEMA(data, alpha = 0.1) {
  const smoothed = [data[0]]
  for (let i = 1; i < data.length; i++) {
    smoothed.push(alpha * data[i] + (1 - alpha) * smoothed[i - 1])
  }
  return smoothed
}
```

**Gaussian Smoothing:**
```javascript
function smoothGaussian(data, sigma = 2) {
  const kernel = gaussianKernel(sigma)
  return convolve(data, kernel)
}
```

### Subsampling Algorithms

**LTTB (Largest Triangle Three Buckets):**
```javascript
function subsampleLTTB(data, threshold) {
  // Downsample to threshold points while preserving shape
  // Much better than naive "every Nth point"
  // Preserves peaks, valleys, trends
}
```

**Why LTTB is better than WandB:**
- WandB: Takes every Nth point (misses important features)
- LTTB: Selects points that preserve visual shape
- Result: 1000-point LTTB looks like 10K points

---

## Data Model

### Metric Storage

**Challenge:** Storing millions of metric points efficiently

**Solution:**
```sql
-- Partitioned by run_id for better query performance
CREATE TABLE metrics (
    ...
) PARTITION BY HASH (run_id);

-- Indexes for fast queries
CREATE INDEX idx_metrics_run_name ON metrics(run_id, name);
CREATE INDEX idx_metrics_timestamp ON metrics(run_id, timestamp);
```

**Batch Inserts:**
```python
# Client buffers 100 metrics, then sends batch
metrics_buffer = []
for step in range(1000):
    metrics_buffer.append({"name": "loss", "value": loss, "step": step})

    if len(metrics_buffer) >= 100:
        kohaku.log_batch(metrics_buffer)
        metrics_buffer = []
```

**Performance:** 100K metrics inserted in ~5 seconds with batch inserts

### Metric Aggregation

**Problem:** 100K metric points × 10 runs = 1M points to send to frontend

**Solution - Server-side downsampling:**
```python
@router.get("/runs/{id}/metrics/{name}")
async def get_metric(run_id, name, max_points=1000):
    count = Metric.select().where(...).count()

    if count <= max_points:
        # Return all points
        return all_metrics

    # Downsample
    step_size = count // max_points
    metrics = Metric.select().where(
        (Metric.step % step_size == 0)
    )
    return metrics
```

---

## Frontend Design (Planned)

### Project Overview Page

```
+----------------------------------+
| Project: bert-finetuning         |
| 15 runs | 3 active | Last: 2h ago |
+----------------------------------+
|                                  |
| [Chart] Training Loss            |
|   - All runs overlaid            |
|   - Smoothing controls           |
|   - Legend with run names        |
|                                  |
+----------------------------------+
| Recent Runs                      |
| ✅ run-001  | 98.5% acc | 2h ago |
| ⏸️  run-002  | Running   | Now    |
| ❌ run-003  | Failed    | 1d ago |
+----------------------------------+
```

### Run Detail Page

```
+----------------------------------+
| Run: experiment-001              |
| Status: Completed | Duration: 2h |
+----------------------------------+
| [Tab] Charts | Config | Artifacts|
+----------------------------------+
|                                  |
| [Chart Grid - 2x2]               |
|  - train/loss                    |
|  - val/loss                      |
|  - train/accuracy                |
|  - val/accuracy                  |
|                                  |
| [Smoothing] [━━━━━━○━] 0.3       |
| [Subsample] 1000 points          |
|                                  |
+----------------------------------+
```

### Run Comparison Page

```
+----------------------------------+
| Comparing 5 runs                 |
+----------------------------------+
| Metric: val/accuracy             |
|                                  |
| [Chart] All runs overlaid        |
|   run-001: 98.5% (best)          |
|   run-002: 97.8%                 |
|   run-003: 96.2%                 |
|   ...                            |
|                                  |
+----------------------------------+
| Hyperparameters                  |
| LR    | BS  | Optimizer | Acc    |
| 0.001 | 32  | Adam      | 98.5%  |
| 0.01  | 64  | Adam      | 97.8%  |
+----------------------------------+
```

---

## Implementation Phases

### Phase 1: Mock Frontend (1 week)

**Goal:** Build UI with mock data to validate design

**Deliverables:**
- Chart components (Plotly.js WebGL)
- Smoothing controls
- Run comparison UI
- Mock data generator

**Tech Stack:**
- Vue 3 + Vite
- Plotly.js (WebGL build)
- Pinia for state management
- UnoCSS for styling

---

### Phase 2: Backend API (1.5 weeks)

**Goal:** Build API for storing and querying metrics

**Deliverables:**
- Database models (Project, Run, Metric, Artifact)
- CRUD endpoints for all entities
- Metric batch insert
- Server-side downsampling
- Token-based authentication

**Tech Stack:**
- FastAPI
- PostgreSQL with Peewee ORM
- Same patterns as KohakuHub (db_operations.py, async_utils.py)

---

### Phase 3: Python Client (1 week)

**Goal:** Build Python library for logging from training scripts

**Deliverables:**
- Context manager for runs (`with kohaku.run()`)
- Metric batching (buffer 100 metrics)
- Artifact upload (S3 presigned URLs)
- PyTorch/TensorFlow callbacks
- Offline mode (cache and sync later)

**Tech Stack:**
- Pure Python (no dependencies except requests)
- Optional: PyTorch, TensorFlow integrations

---

### Phase 4: Frontend Integration (1 week)

**Goal:** Connect frontend to real API

**Deliverables:**
- API client (Axios)
- Real-time updates (SSE)
- Chart persistence
- Export functionality

---

### Phase 5: KohakuHub Integration (1 week)

**Goal:** Optional integration with KohakuHub

**Deliverables:**
- Shared authentication (OAuth or API tokens)
- Auto-create project from repository
- Link runs to commits
- Upload checkpoints to repository
- Embed in repo "Metrics" tab

---

## Project Structure (Planned)

```
src/kohakuboard/                       # Standalone project
├── __init__.py
├── main.py                            # FastAPI app
├── config.py                          # Configuration
├── db.py                              # Database models
├── db_operations.py                   # DB operations (sync)
├── logger.py                          # Logger (reuse from KohakuHub)
├── api/
│   ├── routers/
│   │   ├── projects.py                # Project CRUD
│   │   ├── runs.py                    # Run CRUD + metrics
│   │   ├── metrics.py                 # Metric queries + aggregation
│   │   ├── charts.py                  # Chart configs
│   │   ├── artifacts.py               # Artifact management
│   │   ├── compare.py                 # Run comparison
│   │   └── export.py                  # Export to CSV/JSON
│   └── utils/
│       ├── aggregation.py             # Metric downsampling
│       └── smoothing.py               # Smoothing algorithms
├── auth/
│   ├── dependencies.py                # Auth (minimal or reuse KohakuHub)
│   └── permissions.py                 # Permission checks
└── client/
    ├── __init__.py
    └── kohaku.py                      # Python client library

src/kohakuboard-ui/                    # Standalone frontend
├── src/
│   ├── pages/
│   │   ├── index.vue                  # Project list
│   │   ├── projects/[id]/
│   │   │   ├── overview.vue           # Project overview
│   │   │   ├── runs.vue               # Run list
│   │   │   ├── compare.vue            # Run comparison
│   │   │   └── settings.vue           # Settings
│   │   └── runs/[id].vue              # Run detail
│   ├── components/
│   │   ├── chart/
│   │   │   ├── LineChart.vue          # Plotly line chart
│   │   │   ├── ScatterChart.vue       # Scatter plot
│   │   │   ├── ChartControls.vue      # Smoothing controls
│   │   │   └── ChartGrid.vue          # Multi-chart grid
│   │   ├── run/
│   │   │   ├── RunCard.vue            # Run summary
│   │   │   ├── RunList.vue            # Run list
│   │   │   └── RunComparison.vue      # Side-by-side
│   │   └── common/
│   │       └── MetricTable.vue        # Metric values
│   ├── stores/
│   │   ├── projects.js                # Project store
│   │   ├── runs.js                    # Run store
│   │   └── charts.js                  # Chart config
│   └── utils/
│       ├── api.js                     # API client
│       ├── smoothing.js               # Smoothing algorithms
│       └── subsampling.js             # LTTB, etc.
└── package.json
```

---

## Performance Targets

### Metric Ingestion

| Metric Count | Batch Size | Insert Time | Throughput |
|--------------|------------|-------------|------------|
| 100 | 100 | ~50ms | 2,000/s |
| 1,000 | 100 | ~500ms | 2,000/s |
| 10,000 | 100 | ~5s | 2,000/s |
| 100,000 | 100 | ~50s | 2,000/s |

**Target:** 2,000 metrics/second sustained

### Query Performance

| Query | Metric Count | Response Time |
|-------|--------------|---------------|
| Single metric | 100K | <500ms |
| All metrics | 1M (10 metrics × 100K steps) | <2s |
| Comparison (10 runs) | 1M | <5s |
| Aggregation | 1M | <3s |

**Target:** Sub-second response for single metric queries

### Chart Rendering

| Points | Render Time | Interaction | Memory |
|--------|-------------|-------------|--------|
| 1K | <100ms | Instant | ~5 MB |
| 10K | <200ms | Smooth | ~20 MB |
| 100K | <500ms | Smooth (WebGL) | ~50 MB |
| 1M | ~2s | Smooth (WebGL) | ~200 MB |

**Target:** <500ms render for 100K points

---

## Deployment Options

### Option A: Standalone Container

```yaml
# docker-compose.yml
services:
  kohakuboard:
    image: kohakuboard:latest
    ports:
      - "38080:8000"
    environment:
      - DATABASE_URL=postgresql://...
      - S3_BUCKET=artifacts
    depends_on:
      - postgres
      - minio
```

**Pros:**
- Independent deployment
- Separate scaling
- Can be open-sourced separately

**Cons:**
- Separate authentication
- No KohakuHub integration

---

### Option B: Integrated with KohakuHub

```yaml
services:
  kohakuhub:
    image: kohakuhub:latest
    environment:
      - KOHAKUBOARD_ENABLED=true
    ports:
      - "28080:8000"  # Both services
```

**Pros:**
- Shared authentication
- Unified UI
- Repo-metrics linking

**Cons:**
- Coupled deployment
- Harder to scale separately

---

## Licensing Strategy

### Dual Licensing (Same as Dataset Viewer)

**Kohaku Software License 1.0:**
- Free for non-commercial use
- Commercial trial: 3 months OR $25k revenue/year
- After trial: Paid commercial license required

**Why this license:**
- Allows experimentation and small-scale use
- Prevents large companies from exploiting free work
- Sustainable development model
- Source code must be available (like AGPL)

**Integration with KohakuHub:**
- KohakuHub can integrate (same copyright owner)
- Third-party forks must comply with both licenses
- Or remove KohakuBoard for AGPL-3 only

---

## Comparison with Alternatives

| Feature | WandB | MLflow | KohakuBoard |
|---------|-------|--------|-------------|
| **Hosting** | Cloud only | Self-hosted | Self-hosted |
| **Cost** | $50-200/mo | Free | Free trial, then license |
| **Chart Performance** | Poor (>10K points) | Basic | Excellent (WebGL) |
| **Smoothing** | Poor | Basic | Advanced (EMA, Gaussian) |
| **SQL Queries** | ❌ No | ❌ No | ✅ Yes (via DuckDB) |
| **Model Registry** | Limited | ✅ Yes | Via KohakuHub integration |
| **Source Code** | Closed | Open (Apache 2.0) | Open (Kohaku License) |

---

## Timeline

**Total Development Time:** ~10-12 weeks

**Milestones:**
- Week 1: Mock frontend ✅ Validate design
- Week 2-3: Backend API ✅ Working metrics storage
- Week 4: Python client ✅ Can log from training
- Week 5: Frontend integration ✅ Real-time charts
- Week 6: KohakuHub integration ✅ Unified platform
- Week 7-8: Polish, testing, documentation
- Week 9-10: Beta testing with real users
- Week 11-12: Bug fixes, performance optimization

**Launch:** Q2 2025 (estimated)

---

## Current Status

**Phase:** Planning / Design
**Code:** Not yet implemented
**Documentation:** This document

**Next steps:**
1. Finalize design (gather feedback)
2. Create mockups (Figma or similar)
3. Start Phase 1 (mock frontend)
4. Iterate based on feedback

---

## Contributing

**Interested in KohakuBoard development?**

**Ways to contribute:**
- Design feedback (UI/UX suggestions)
- Feature requests (what do you need from WandB replacement?)
- Beta testing (when ready)
- Code contributions (when open-sourced)

**Contact:** kohaku@kblueleaf.net

---

## FAQ

### Q: When will KohakuBoard be available?

**A:** Estimated Q2 2025. Check roadmap for updates.

### Q: Can I use it without KohakuHub?

**A:** Yes! It's designed as a standalone project that can optionally integrate.

### Q: Will it be compatible with WandB?

**A:** Partially. Same concepts (projects, runs, metrics) but different API. Migration tools planned.

### Q: How is it better than WandB?

**A:** Better chart performance, better smoothing/subsampling, self-hosted, no vendor lock-in.

### Q: What about TensorBoard?

**A:** TensorBoard is great for local dev. KohakuBoard is for team collaboration, long-term storage, and production monitoring.

### Q: Can I import WandB data?

**A:** Planned for Phase 3. Will support importing WandB exports.

---

## Conclusion

KohakuBoard aims to provide a better, self-hosted alternative to WandB with focus on performance, user experience, and data ownership. It's designed to work standalone or integrate seamlessly with KohakuHub for unified ML workflows.

**Stay tuned for updates!**

**Questions?** Contact kohaku@kblueleaf.net or check the roadmap.
