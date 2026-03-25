# Implementing Enterprise-Grade CI/CD for Microsoft Fabric — A Technical Deep Dive

*How to build a production-ready, team-scoped deployment pipeline using fabric-cicd, Azure DevOps, and Fabric-native Variable Libraries*

---

## Introduction

Microsoft Fabric has rapidly matured as an end-to-end analytics platform, but one area that still challenges many teams is **how to operationalize deployments**. Without a proper CI/CD strategy, teams resort to manual workspace promotion, inconsistent environments, and configuration drift between DEV, UAT, and PROD — exactly the kind of technical debt that slows data engineering teams down.

This article walks through a production-grade Fabric CI/CD implementation built on:

- **`fabric-cicd`** — the Microsoft-backed open-source deployment tool
- **Azure DevOps (ADO)** — pipelines, variable groups, and Git integration
- **Fabric-native Variable Libraries** — runtime, environment-aware parameterization for items that support it (Notebooks, Data Pipelines, Copy Jobs, Shortcuts, Dataflows)
- **`parameter.yml`** — deployment-time parameterization via `fabric-cicd` for items that do not yet support Variable Libraries (Fabric Environments, Semantic Models)
- **Cherry-pick based branch promotion** — for safe, deliberate environment progression

> **On parameterization:** the recommended approach is to use **Variable Libraries wherever supported** and fall back to **`parameter.yml`** for items that don't support them yet. Most teams will run both in parallel until Microsoft extends Variable Library support to all Fabric item types.

This is not a "getting started" walkthrough. It is a technical reference for data engineers and architects who want to understand the *why* behind each design decision — including the gotchas we hit in production.

---

## Architecture Overview

The deployment model is built around three core principles:

1. **One repository per team** — Each team (e.g. FrontOffice, BackOffice, Enterprise) maintains its own dedicated ADO repository. This is not just an organizational preference — it is a deliberate architectural boundary that eliminates cross-team Git merge conflicts, prevents one team's changes from accidentally landing in another team's deployment pipeline, and keeps the blast radius of any failed deployment scoped to a single team. Separate repos also simplify branch policies, cherry-pick promotions, and audit trails, since every commit on a branch belongs unambiguously to one team.
2. **Full deployment every time** — `fabric-cicd` deploys everything in the repo's scope on every pipeline run; there is no selective item deployment (though `item_type_in_scope` narrows scope by type)
3. **Four-environment separation via Fabric workspaces** — Feature, DEV, UAT, and PROD are distinct Fabric workspaces. Feature and DEV workspaces are connected to their corresponding Git branches via Fabric's native Git integration. UAT and PROD workspaces are not connected to any branch — `fabric-cicd` deploys items directly from the `uat` and `main` branch project folder in the repo to the corresponding workspace at pipeline runtime.

The full workspace-to-branch mapping looks like this:

```
ADO Git Repository (FabricTeamRepo)
        │
        ├── feature/<n> ◄──────────────────► Feature Workspace(s)
        │                [Fabric native Git sync — bidirectional]
        │
        ├── dev ◄──────────────────────────► DEV Workspace
        │                [Fabric native Git sync — bidirectional]
        │
        ├── uat ──── ADO pipeline reads repo ────► UAT Workspace
        │                [fabric-cicd, no Git sync on workspace]
        │
        └── main ─── ADO pipeline reads repo ────► PROD Workspace
                         [fabric-cicd, no Git sync on workspace]
```

**Two distinct integration mechanisms are in play:**

- **Feature and DEV workspaces** use **Fabric's native Git integration** — a bidirectional sync between each workspace and its corresponding branch. Developers create and modify Fabric items directly in their Feature workspace UI, then commit those changes to their feature branch using Fabric Git Sync. When the feature is ready, changes are merged from the feature branch into the `dev` branch via a PR — at which point a developer manually syncs the DEV workspace from the `dev` branch using Fabric Git Sync. No ADO pipeline is involved at this layer.

> **Note:** This manual sync step can be automated using the [Fabric Git Sync API](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/update-from-git) — triggering an `updateFromGit` call as part of the ADO pipeline after the PR merges into `dev`. For this implementation, the sync is kept manual.
- **UAT and PROD workspaces are not connected to any Git branch.** When a PR merges into `uat` or `main`, the ADO pipeline checks out the repo at that point and runs `fabric-cicd` to push item definitions directly to the target workspace. These workspaces never pull from Git — they only receive items when a pipeline explicitly runs.

This separation is deliberate: the feature layer is optimized for developer speed and iteration; the UAT/PROD layer is optimized for stability and governance — workspaces receive deployments only when a pipeline runs, never via passive Git sync.

---

## Repository Structure

The repository is organized into two top-level workspace subdirectories — `engineering` for all data engineering Fabric items, and `presentation` for semantic models and reports. Deployment scripts and pipelines are kept outside `workspace/` so they are never treated as deployable Fabric items.

```
FabricTeamRepo/                          ← repo root (one per team)
├── .deploy/
│   └── deploy_fabric_workspace.py       ← deployment script
├── .pipelines/
│   ├── deploy_workspace_uat.yml         ← UAT pipeline
│   └── deploy_workspace_prod.yml        ← PROD pipeline
├── workspace/
│   ├── engineering/                     ← all data engineering Fabric items
│   │   ├── project_one/                 ← project directory
│   │   │   ├── copyjobs/
│   │   │   ├── dataflowsGen2(CICD)/
│   │   │   ├── environments/
│   │   │   ├── lakehouses/
│   │   │   ├── notebooks/
│   │   │   ├── pipelines/
│   │   │   ├── variablelibraries/
│   │   │   ├── parameter.yml            ← environment parameterization
│   │   │   └── README.md
│   │   └── project_two/                 ← another project directory
│   │       └── README.md
│   └── presentation/                    ← semantic models and reports
│       ├── project_one/                 ← project directory
│       │   ├── semantic_models_reports/
│       │   ├── variablelibraries/
│       │   ├── parameter.yml            ← environment parameterization
│       │   └── README.md
│       └── project_two/                 ← another project directory
│           ├── semantic_models_reports/
│           ├── variablelibraries/
│           ├── parameter.yml
│           └── README.md
├── .gitignore
└── README.md
```

**Key design decisions:**

- `.deploy/` and `.pipelines/` are kept outside `workspace/` intentionally — `fabric-cicd` deploys everything under `workspace/`, and deployment scripts should not be treated as Fabric items
- **`workspace/engineering/`** contains all data engineering Fabric items — notebooks, pipelines, lakehouses, environments, copy jobs, dataflows, and variable libraries. Each team's project goes into its own directory under `engineering/`
- **`workspace/presentation/`** contains semantic models and reports — kept separate from engineering items for cleaner scope management and independent deployments
- `parameter.yml` lives alongside the Fabric items it parameterizes — scoped to that project directory, not the repo root
- **Feature branches share the same `workspace/` folder structure** — when a developer syncs their Feature workspace to a feature branch via Fabric Git Sync, items land in the same directory paths. This makes merging from feature → dev clean and predictable, with no structural divergence between branches

---

## Branch Strategy and Promotion Flow

The promotion model operates in two layers — developers iterate freely in Feature and DEV workspaces using Fabric Git Sync, while UAT and PROD deployments are controlled, pipeline-driven promotions triggered by deliberate PR merges.

### The Four Branch Types

**`feature/<n>`** — Long-lived. Connected to each developer's personal Feature workspace via Fabric Git Sync (bidirectional). Developers commit changes from the workspace to this branch and pull updates back. Maintains full history.

**`dev`** — Long-lived. Connected to the shared DEV workspace via Fabric Git Sync (bidirectional). This is the central integration point — all feature branches target `dev` first.

**`uat`** — Long-lived. Not connected to any workspace via Git. When a PR merges into `uat`, the ADO pipeline reads the repo and deploys item definitions directly to the UAT workspace using `fabric-cicd`.

**`main`** — Long-lived. Not connected to any workspace via Git. Same model as `uat` — ADO pipeline reads the repo and deploys directly to the PROD workspace on PR merge. Policy-gated with stricter approvals.

### One Rule Across All Promotions — Always Squash Merge

Regardless of which branch transition you are performing — `feature → dev`, `dev → uat`, or `uat → main` — **always use Squash Merge, never a regular commit merge.**

Squash merge collapses all commits from the source branch into a single, clean commit on the target branch. This keeps the history of `dev`, `uat`, and `main` concise and readable — each commit on these branches represents a complete, reviewable unit of work rather than a noisy stream of granular development commits. It also makes cherry-picking significantly simpler, since each promotion is represented by a single commit SHA rather than a range of commits to track.

> ✅ `feature → dev` — Squash merge
> ✅ `promote/dev-to-uat → uat` — Squash merge
> ✅ `promote/uat-to-prod → main` — Squash merge

---

### Phase 1 — Developer Inner Loop (Feature and DEV workspaces via Git Sync)

Both Feature and DEV workspaces use **Fabric's native Git integration** — a fully bidirectional sync between the workspace and its branch. No ADO pipeline is involved at this layer:

- Developer builds or modifies items directly in the Fabric UI
- They commit changes from the workspace to the branch using Fabric Git Sync
- They can also pull changes from the branch back into the workspace at any time

```
Feature Workspace  ◄──── Sync to Git ────►  feature/<n> branch
                         (bidirectional)

DEV Workspace      ◄──── Sync to Git ────►  dev branch
                         (bidirectional)
```

Feature branches are always created **from `dev`** — not from `uat` or `main` — to ensure developers start from the latest integrated state:

```bash
# Always base feature branches off the latest dev
git checkout dev && git pull origin dev
git checkout -b feature/my-feature

# ... developer works in Feature workspace, syncing to this branch ...

# When ready, open PR: feature/my-feature → dev
# Use Squash merge — do NOT delete the source branch (feature branches are long-lived)
```

When the PR merges into `dev`, a developer manually syncs the DEV workspace from the `dev` branch using Fabric Git Sync. This step can also be automated using the [Fabric Git Sync API](https://learn.microsoft.com/en-us/rest/api/fabric/core/git/update-from-git) — triggering an `updateFromGit` call as part of the ADO pipeline immediately after the PR merge.

```
feature/<n> ──── PR merge (squash) ────► dev branch
                                                │
                                      Fabric Git Sync (manual)
                                                │
                                                ▼
                                       DEV Fabric Workspace
```

### Squash Merge + Sync Feature Branch Back to Dev

Because feature branches are long-lived, there is one critical step after every squash merge into `dev` — **merge `dev` back into the feature branch immediately**.

**Why this matters:** A squash merge collapses all feature commits into a single commit on `dev`. The feature branch itself still carries its original granular commits. If the developer continues working on the feature branch without merging `dev` back, the next PR will look noisy — it will appear to contain all the previously merged commits again, even though the actual diff is small. Merging `dev` back into the feature branch aligns the histories and ensures future PRs only surface genuinely new work.

```bash
# Step 1 — Open PR: feature/my-feature → dev
# Complete with Squash merge (do NOT delete source branch)

# Step 2 — Immediately merge dev back into the feature branch
git checkout feature/my-feature
git merge origin/dev

# Step 3 — Push the updated feature branch
git push origin feature/my-feature

# Step 4 — Sync the Feature workspace with the updated branch
# via Fabric Git Sync → Update all
```

After this, the feature branch history is aligned with `dev` — future commits and PRs from this branch will only contain new work going forward.

### How Developer Collaboration Works Through the DEV Workspace

The `dev` branch and DEV workspace are the **central integration point** for the team. Multiple developers work in parallel, each in their own Feature workspace and feature branch. Here is how they stay in sync with each other:

**Developer A finishes their feature and merges into dev:**
```bash
# Developer A opens PR: feature/developerA → dev (squash merge)
# PR is reviewed and merged
# Developer manually syncs the DEV workspace from the dev branch using Fabric Git Sync
# (this can also be automated via the Fabric Git Sync API — updateFromGit)
# All team members can now see the latest changes in the DEV workspace
```

**Developer B pulls the latest dev changes into their Feature workspace:**
```bash
# Developer B updates their local feature branch with the latest dev
git checkout feature/developerB
git merge origin/dev
# or rebase:
git rebase origin/dev

# Then pulls the updated branch into their Feature workspace
# via Fabric Git Sync → Update all / Pull
```

> **Rebase caveat:** `git rebase` rewrites commit SHAs, producing a clean linear history without merge commits. However, if two developers share the same feature branch, rebase will cause their copies to diverge — use `git merge` in that case. Since feature branches in this model are per developer (`feature/developerA`, `feature/developerB`), rebase is generally safe as no one else is working on the same branch.

This pull step is important — it keeps each developer's Feature workspace aligned with the team's integrated state in DEV, reducing merge conflicts and ensuring no one builds on top of stale items.

**The DEV workspace acts as a shared preview environment:**

- Any developer or stakeholder can open the DEV workspace to see the latest integrated state of all merged features
- Items in DEV reflect the exact state of the `dev` branch — because the workspace is Git-synced, there is no drift between what is in the branch and what is in the workspace
- If a developer needs to test how their feature interacts with another developer's recently merged work, they simply pull `dev` into their feature branch and sync to their Feature workspace

**Merge conflicts during collaboration:**

If two developers modify the same Fabric item (e.g. the same notebook or semantic model definition), a merge conflict will surface when the second developer opens a PR into `dev`. The conflict must be resolved in the `.tmdl` / `.json` definition files in the repo before the PR can complete — the same way code conflicts are resolved in any standard Git workflow. After resolving and merging, the DEV workspace syncs automatically.

---

### Phase 2 — UAT Promotion via Cherry-Pick (dev → uat)

Once changes are integrated and validated in DEV, they are promoted to UAT via a **cherry-pick promotion branch** — not a direct merge from `dev` to `uat`. This prevents all accumulated dev commits (including unfinished work from other developers) from landing in UAT.

```bash
# 1. Start from the latest remote uat branch
git checkout uat && git pull origin uat

# 2. Create a short-lived promotion branch from uat
git checkout -b promote/dev-to-uat/2026.03.14

# 3. Identify the squash commit(s) on dev to promote
git log --oneline origin/dev -n 20

# 4. Cherry-pick the validated commit(s)
git cherry-pick <sha>
# For multiple commits:
git cherry-pick <sha1> <sha2> <sha3>

# If conflicts occur:
git status
# fix conflicts...
git add <files>
git cherry-pick --continue
# Abort if needed: git cherry-pick --abort

# 5. Push and open PR into uat
git push -u origin promote/dev-to-uat/2026.03.14
```

**PR title convention:** `Promote DEV → UAT: 2026.03.14`

**PR completion settings:**
- ✅ Squash merge
- ✅ Delete source branch after merge

When the PR merges into `uat`, the **UAT ADO pipeline** triggers and deploys to the **UAT Fabric workspace** for business/QA validation.

---

### Phase 3 — PROD Promotion via Cherry-Pick (uat → main)

Once changes are validated in UAT, they are promoted to PROD using the same cherry-pick pattern. When the PR into `main` merges, the ADO PROD pipeline checks out the `main` branch and runs `fabric-cicd` to deploy directly to the PROD workspace. Like UAT, the PROD workspace has no Git sync connection — it receives items only via the pipeline.

```bash
# 1. Start from the latest remote main branch
git checkout main && git pull origin main

# 2. Create a short-lived promotion branch from main
git checkout -b promote/uat-to-prod/2026.03.14

# 3. Identify the squash commit(s) on uat to promote
git log --oneline origin/uat -n 20

# 4. Cherry-pick the validated commit(s)
git cherry-pick <sha>

# 5. Push and open PR into main
git push -u origin promote/uat-to-prod/2026.03.14
```

**PR title convention:** `Promote UAT → PROD: 2026.03.14`

When the PR merges into `main`, the **PROD ADO pipeline** triggers and deploys to the **PROD Fabric workspace**.

---

### The Full Flow End-to-End

```
Developer builds in Feature Workspace
         │
         │  Fabric native Git sync (bidirectional)
         ▼
feature/<n> branch  ◄────────────────────────────────  Feature Workspace
         │                  (bidirectional Git sync)
         │  PR → dev  (squash merge)
         ▼
dev branch  ◄────────────────────────────────────────  DEV Workspace
         │          (bidirectional Git sync)
         │
         │  Cherry-pick via promote/dev-to-uat/<release>
         │  PR → uat  (squash merge)
         ▼
uat branch  ──── ADO pipeline reads repo ────────────► UAT Workspace
         │       (fabric-cicd, no Git sync on workspace)
         │
         │  Cherry-pick via promote/uat-to-prod/<release>
         │  PR → main  (squash merge)
         ▼
main branch ──── ADO pipeline reads repo ────────────► PROD Workspace
                  (fabric-cicd, no Git sync on workspace)
```

### Why Cherry-Pick for UAT and PROD Promotion?

Because `fabric-cicd` performs a **full deployment every time** — it deploys *everything* in the repository directory to the target workspace. A regular merge from `dev` into `uat` or `main` promotes the **entire branch state**, not just what's ready — making it impossible to ship only validated changes and turning rollbacks into reverting large merge commits instead of a single clean PR unit.

Cherry-pick into a promotion branch gives surgical control: each PR becomes a clean, intentional, independently revertable unit. You can move it forward or roll it back without touching anything else.

### Why Not Cherry-Pick Directly Onto the Environment Branch?

Cherry-picks are never applied directly onto `uat` or `main`. Instead they are cherry-picked into a **short-lived promotion branch** (`promote/dev-to-uat/<release>`, `promote/uat-to-prod/<release>`), then merged via a PR — and that PR is squash-merged too. This keeps the target branch history clean and ensures every change on `uat` and `main` went through a proper review and approval gate, not a silent direct commit.

### Dev, UAT, and PROD Are Intentionally Different — That's by Design

There is no expectation that `dev`, `uat`, and `main` stay in sync or have aligned histories. They are **independent environment branches** with intentionally different histories. Promotion is not "moving the same commit forward" — it is creating a new PR and commit in the target branch that represents **approval for that environment**.

This enables a clean evolving feature pattern. A single feature might have:

```
dev:   7 PRs  (iterative development, rapid commits)
uat:   2 PRs  (consolidated, validated increments)
main:  1 PR   (single approved unit representing the full feature)
```

The PROD PR naturally becomes the superset of all approved changes — without any of the development noise.

### The Only Divergences That Actually Matter

You do not need to keep `dev`, `uat`, and `main` in lockstep. The only two divergences worth actively managing are:

- **Feature branch vs `dev`** — keep feature branches rebased on `dev` so PRs stay focused on new work only. As other developers merge into `dev`, your feature branch falls behind. Rebasing replays your commits on top of the latest `dev`, ensuring your PR shows only your actual changes — not noise from commits you haven't seen yet.

```bash
# Keep your feature branch up to date with dev
git checkout feature/developerB
git rebase origin/dev
git push --force-with-lease origin feature/developerB

# Then sync your Feature workspace via Fabric Git Sync → Update all
```

- **Promotion branches vs their target** — always create promotion branches from the latest `uat` or `main`. This ensures the promotion branch is ahead of its target with only the new cherry-picked commit(s), avoiding conflicts from commits already in the target branch that your promotion branch doesn't know about.

```bash
# Always start a promotion branch from the latest target
git checkout uat && git pull origin uat
git checkout -b promote/dev-to-uat/release
git cherry-pick <sha>   # add only what you want to promote
```

Everything else — `dev` vs `uat`, `uat` vs `main` — diverges intentionally. `uat` only contains what has been explicitly promoted and validated. `main` only contains what has been approved for production. The gap between them is not a problem to fix — it is the model working as designed. Optimize for **clean, auditable promotion and safe rollback**, not branch alignment.


---
## The Deployment Script

The Python deployment script (`deploy_fabric_workspace.py`) is intentionally minimal — it delegates all deployment logic to the `fabric-cicd` library and accepts all configuration via CLI arguments passed from the ADO pipeline.

```python
import sys
import os
import argparse
from pathlib import Path

from azure.identity import AzureCliCredential
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items, \
    change_log_level, append_feature_flag

# Force unbuffered output — critical for real-time ADO pipeline log streaming
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.stderr.reconfigure(line_buffering=True, write_through=True)

# Enable DEBUG logging when SYSTEM_DEBUG is set in the ADO pipeline
if os.getenv("SYSTEM_DEBUG", "false").lower() == "true":
    change_log_level("DEBUG")

root_directory = Path(__file__).resolve().parent.parent

# Enable shortcut publishing support
append_feature_flag("enable_shortcut_publish")

parser = argparse.ArgumentParser()
parser.add_argument('--workspace_id', type=str)
parser.add_argument('--environment', type=str)
parser.add_argument('--repository_directory', type=str)
parser.add_argument('--item_type_in_scope', type=str)
args = parser.parse_args()

# AzureCliCredential works both locally (developer az login) and in ADO pipelines
# (via AzureCLI@2 task which injects a valid token automatically)
token_credential = AzureCliCredential()

target_workspace = FabricWorkspace(
    workspace_id=args.workspace_id,
    environment=args.environment,
    repository_directory=args.repository_directory,
    item_type_in_scope=args.item_type_in_scope.split(","),
    token_credential=token_credential,
)

publish_all_items(target_workspace)

# Uncomment only when orphan cleanup is explicitly required — see caution below
# unpublish_all_orphan_items(target_workspace)
```

### Key Design Decisions

**`AzureCliCredential`** — This is the authentication bridge between the ADO pipeline and Fabric. When the pipeline runs inside the `AzureCLI@2` task, Azure CLI is already authenticated via the service connection. `AzureCliCredential` picks up that token automatically — no secrets or client credentials needed in the script itself.

**`append_feature_flag("enable_shortcut_publish")`** — Enables OneLake shortcut deployment support, which is in preview in `fabric-cicd`. Include this if your workspace contains shortcuts.

**`unpublish_all_orphan_items` — When it makes sense and when to be cautious**

`unpublish_all_orphan_items` removes Fabric items that exist in the target workspace but are **absent from the repo**. It enforces the repo as the single source of truth — anything not in Git gets removed from the workspace.

**When it makes sense:**
- You have deliberately removed an item from the repo (e.g. a deprecated report or notebook) and want it automatically cleaned up from the workspace on the next deployment
- You want strict workspace governance — no items should exist in production unless they are tracked in Git
- You are doing a clean initial deployment and want to ensure the workspace exactly mirrors the repo state

**⚠️ When to be cautious — use with care:**
- A developer creates a new item directly in the Fabric workspace (outside Git) and hasn't committed it yet — it will be **deleted silently** on the next pipeline run
- A file is accidentally omitted from the repo due to a bad commit or `.gitignore` misconfiguration — the corresponding workspace item gets deleted
- Items managed by other teams or processes that live in the same workspace but outside the repo scope could be affected
- In production environments, the risk of accidental deletion outweighs the cleanup benefit in most cases

**Our approach:** `unpublish_all_orphan_items` is intentionally commented out by default. Enable it only when you have full confidence that the repo is the complete and accurate representation of everything that should exist in the workspace — and preferably test in DEV or UAT first before enabling in PROD.

**`--item_type_in_scope`** — this ADO variable controls what gets deployed. Running a full deployment on every pipeline run is expensive; scoping to only the changed item types significantly reduces deployment time.

Supported types: `VariableLibrary, Environment, Notebook, DataPipeline, Lakehouse, CopyJob, Dataflow, SemanticModel, Report`

---

## The ADO Pipeline

Two separate YAML files exist — one for UAT (`deploy_workspace_uat.yml`) and one for PROD (`deploy_workspace_prod.yml`). Each references its own ADO Variable Group.

```yaml
trigger: none  # Manual or PR-merge triggered only

variables:
  - group: Fabric_Deployment_Group_UAT  # Switch to _PROD for the prod pipeline

stages:
  - stage: Build
    jobs:
      - job: Build
        pool:
          name: Default
        steps:
          - checkout: self
          - task: PublishPipelineArtifact@1
            inputs:
              targetPath: '$(System.DefaultWorkingDirectory)'
              artifact: build

  - stage: Release
    dependsOn: Build
    jobs:
      - job: Release
        pool:
          name: Default
        steps:
          - checkout: none

          - task: PowerShell@2
            displayName: 'Clean build folder'
            inputs:
              targetType: 'inline'
              script: |
                $p = '$(Pipeline.Workspace)\build'
                if (Test-Path $p) { Remove-Item $p -Recurse -Force }

          - task: DownloadPipelineArtifact@2
            inputs:
              artifact: build
              path: '$(Pipeline.Workspace)\build'

          - task: UsePythonVersion@0
            inputs:
              versionSpec: '3.12.10'
              addToPath: true

          - script: |
              # Installing from private JFrog Artifactory (enterprise use case)
              # If you don't have a private registry, install directly from PyPI:
              # python -m pip install --upgrade fabric-cicd azure-identity
              python -m pip install --upgrade -i https://<your-artifactory>/api/pypi/<your-feed>/simple fabric-cicd azure-identity
              python -m pip show fabric-cicd
              python -m pip show azure-identity
            displayName: 'Install fabric-cicd and azure-identity'

          - task: AzureCLI@2
            displayName: 'Deploy Fabric Workspace'
            inputs:
              azureSubscription: '<your-azure-service-connection>'
              scriptType: 'ps'
              scriptLocation: 'inlineScript'
              inlineScript: |
                $script = "$(Pipeline.Workspace)/build/.deploy/deploy_fabric_workspace.py"
                python -u "$script" `
                  --workspace_id "$(workspace_guid)" `
                  --environment "$(fabric_vl_vset_active)" `
                  --item_type_in_scope "$(fabric_item_type_inscope)" `
                  --repository_directory "$(Pipeline.Workspace)/build/workspace/engineering/$(repo_project_directory)"
```

### Why `python -m pip` and Not Plain `pip`

This is one of the most common silent failures in ADO Python pipelines. ADO agents often have multiple Python installations, and plain `pip` may resolve to a different interpreter than the one `UsePythonVersion` configured. Using `python -m pip` guarantees that packages install into exactly the same interpreter that will run the script — eliminating version mismatch errors that produce cryptic `ModuleNotFoundError` failures at runtime.

### Why Private JFrog Artifactory?

Enterprise environments often restrict public PyPI access for security and compliance reasons. Pulling `fabric-cicd` from a JFrog Artifactory virtual repository (which mirrors PyPI) ensures:
- All packages are scanned and approved
- No direct internet access required from the agent
- Repeatable builds regardless of PyPI availability

If your ADO agent has direct internet access and no private registry requirement, you can install directly from PyPI — no `-i` flag needed:

```bash
python -m pip install --upgrade fabric-cicd azure-identity
```

### The Build/Release Stage Split

The pipeline separates Build (checkout + artifact publish) from Release (download artifact + deploy). This pattern:
- Guarantees the exact same code artifact is deployed, regardless of any concurrent commits to the branch
- Supports future gate approvals between stages (e.g. manual approval before PROD)
- Makes pipeline logs cleaner — build failures are immediately distinguishable from deployment failures

---

## ADO Variable Groups

Each environment has a dedicated ADO Variable Group:

- `Fabric_Deployment_Group_UAT`
- `Fabric_Deployment_Group_PROD`

**`workspace_guid`** — The target Fabric workspace ID. Set per environment in each variable group.

**`fabric_vl_vset_active`** — The active Variable Library value set, e.g. `UAT` or `PROD`. Passed as the `--environment` argument to the deployment script, which activates the matching value set in the Fabric Variable Library during deployment.

**`repo_project_directory`** — The project subdirectory under `workspace/engineering/` or `workspace/presentation/` to deploy. Scopes the deployment to a single team's project.

**`fabric_item_type_inscope`** — Controls which Fabric item types are deployed, e.g. `SemanticModel,Report,Notebook`. Scoping to only the changed item types significantly reduces deployment time.

The key variable here is `fabric_vl_vset_active` — this is what makes the Fabric Variable Library environment-aware. By passing `UAT` or `PROD` as the `--environment` argument, `fabric-cicd` activates the corresponding value set in the Variable Library during deployment.

---

## The `parameter.yml` File

The `parameter.yml` file is the **item parameterization mechanism** in `fabric-cicd`. It tells the deployment tool how to translate environment-agnostic item definitions committed to Git into environment-specific configurations in the target workspace.

### When to Use `parameter.yml` vs Variable Libraries

Not all Fabric items support parameterization through the Fabric Variable Library. This determines which tool you reach for:

Items that support Variable Library — use VL, no `parameter.yml` needed:
- Notebook
- Data Pipeline
- Lakehouse Shortcuts
- Copy Job
- Dataflow Gen2

Items that do not yet support Variable Library — `parameter.yml` is your only option:
- **Fabric Environment**
- **Semantic Model**

**The rule is simple: use Variable Libraries wherever supported. For items that don't support VL — currently Fabric Environments and Semantic Models — `parameter.yml` is your only option.**

This will likely be a mixed approach for most teams in the near term. Microsoft is actively expanding Variable Library support across all Fabric item types, but until full coverage is reached, you need both tools working together:

- **Variable Library** → handles runtime parameterization for notebooks, pipelines, copy jobs, shortcuts — values are read at execution time, no redeployment needed to change them
- **`parameter.yml`** → handles deployment-time parameterization for Environments and Semantic Models — values are applied by `fabric-cicd` at the point of deployment, baked into the item definition in the target workspace

### How It Works

`parameter.yml` works in conjunction with the `environment` argument passed into the `FabricWorkspace` object — which in your ADO pipeline comes from the `fabric_vl_vset_active` variable group variable (e.g. `UAT` or `PROD`). During deployment, `fabric-cicd` reads the file and applies replacements only for the matching environment key. If the environment value is not found in `parameter.yml`, any dependent replacements are silently skipped — no error is raised.

> **Location:** `parameter.yml` must sit in the **root of the `repository_directory`** folder specified in the `FabricWorkspace` object — not the repo root.

### The Four Supported Sections

#### 1. `find_replace` — String substitution across any item definition

Performs a literal string find-and-replace across all item definition files in the repo. Useful for replacing Lakehouse IDs, workspace IDs, connection strings, or any hardcoded value that differs between environments:

```yaml
find_replace:
  - find_value: "your-dev-lakehouse-id"
    replace_value:
      UAT: "uat-lakehouse-id"
      PROD: "prod-lakehouse-id"
```

#### 2. `key_value_replace` — JSONPath-targeted value replacement

Targets a specific key within a JSON-based item definition using a JSONPath expression. Useful for replacing named variable values inside Data Pipelines or other structured definition files:

```yaml
key_value_replace:
  - find_key: $.variables[?(@.name=="Environment")].value
    replace_value:
      UAT: "UAT"
      PROD: "PROD"
```

#### 3. `spark_pool` — Spark pool remapping

Remaps Spark pool instance IDs to environment-appropriate capacity pools — for example, a smaller pool in UAT and a larger one in PROD:

```yaml
spark_pool:
  - instance_pool_id: "your-dev-pool-instance-id"
    replace_value:
      UAT:
        type: "Capacity"
        name: "UAT-Pool-name"
      PROD:
        type: "Capacity"
        name: "PROD-Pool-name"
```

#### 4. `semantic_model_binding` — Data source rebinding

Rebinds semantic models to environment-specific shared cloud connections. Supports a `default` binding (applies to all models not explicitly listed) and model-specific overrides:

```yaml
semantic_model_binding:
  default:
    connection_id:
      UAT: "uat-connection-id"
      PROD: "prod-connection-id"
  models:
    - semantic_model_name: "My Semantic Model"
      connection_id:
        UAT: "uat-connection-id"
        PROD: "prod-connection-id"
```

Without this section, a semantic model deployed to PROD would remain bound to the DEV/UAT data source connection — a silent but critical misconfiguration that causes refresh failures.

### Complete `parameter.yml` Reference

```yaml
find_replace:
  - find_value: "your-dev-lakehouse-id"
    replace_value:
      UAT: "uat-lakehouse-id"
      PROD: "prod-lakehouse-id"

key_value_replace:
  - find_key: $.variables[?(@.name=="Environment")].value
    replace_value:
      UAT: "UAT"
      PROD: "PROD"

spark_pool:
  - instance_pool_id: "your-dev-pool-instance-id"
    replace_value:
      UAT:
        type: "Capacity"
        name: "UAT-Pool-name"
      PROD:
        type: "Capacity"
        name: "PROD-Pool-name"

semantic_model_binding:
  default:
    connection_id:
      UAT: "UAT-connection_id"
      PROD: "PROD-connection_id"
  models:
    - semantic_model_name: "My Semantic Model"
      connection_id:
        UAT: "UAT-connection_id"
        PROD: "PROD-connection_id"
```

---

## Fabric Variable Libraries — Native Environment Parameterization

The Fabric Variable Library is a **Fabric-native item** (not an ADO concept) that provides centralized, environment-aware configuration for notebooks, pipelines, and other Fabric items. It reduces hardcoding and provides a single place to manage environment-specific values.

### Recommended Structure: Two-Library Model

Rather than one monolithic library, split into two libraries by concern:

**`VL_AppConfig`** — Paths, feature flags, table names, and workspace constants. Non-secret values that control application behaviour.

**`VL_ConnectionsAndRefs`** — GUIDs, item references, and connection identifiers. Values that point to environment-specific Fabric items and connections.

### Value Sets

Each library has a **Default** value set and optional named value sets:

```
VL_AppConfig
├── Default          ← fallback values
├── FEATURE          ← developer/feature workspace
├── DEV              ← dev workspace
├── UAT              ← uat workspace
└── PROD             ← prod workspace
```

Only one value set is active at a time in a workspace. Switching the active set changes which values all consumers (notebooks, pipelines) see — with no code changes required.

### How `fabric_vl_vset_active` Connects Everything

The ADO variable `fabric_vl_vset_active` passes the environment name (`UAT` or `PROD`) to `fabric-cicd` as the `--environment` argument. During deployment, `fabric-cicd` activates the matching value set in the Variable Library — ensuring notebooks and pipelines in the target workspace automatically use the correct environment's configuration.

```
ADO Variable Group (UAT)
  fabric_vl_vset_active = "UAT"
         │
         ▼
deploy_fabric_workspace.py
  --environment "UAT"
         │
         ▼
fabric-cicd activates VL value set "UAT"
         │
         ▼
All notebooks/pipelines read UAT values
```

### Supported Variable Types

- **`String`** — paths, names, table names
- **`Boolean`** — feature flags
- **`Integer` / `Number`** — thresholds and limits
- **`DateTime`** (ISO 8601 UTC) — watermarks and cutoff dates
- **`Guid`** — workspace IDs and item references
- **`Item reference`** *(Preview)* — Fabric item pointer using workspaceId + itemId

### Best Practice: Always Add Descriptions

Every variable in the library should have a `Note` (description) field set. This is not enforced by Fabric, but it significantly reduces confusion during troubleshooting, code reviews, and handovers — especially when a variable's name alone doesn't make its purpose obvious (e.g. `vl_wh_gold_id` is ambiguous without a note explaining it's the Gold Warehouse item ID).

---

## What `fabric-cicd` Deploys (and What It Doesn't)

Understanding the deployment scope prevents surprises:

The following item types are fully supported: Notebook, Data Pipeline, Semantic Model, Report, Variable Library, Environment, Copy Job, and Dataflow Gen2. Lakehouse is supported but deploys metadata only — no data is copied. OneLake Shortcuts are supported with the `enable_shortcut_publish` feature flag enabled. Warehouse support is currently limited.

**Important:** `fabric-cicd` deploys item *definitions* — not data. Lakehouses are created as empty containers; data loading remains the responsibility of pipelines and notebooks.

---

## Conclusion

A production-grade Fabric CI/CD implementation is not just about running `fabric-cicd` from a pipeline. It is about:

- A **deliberate branch strategy** that prevents accidental deployments
- **Environment isolation** through separate workspaces and ADO variable groups
- **Native parameterization** via Fabric Variable Libraries that eliminate environment-specific hardcoding
- **Secure authentication** via `AzureCliCredential` and service connections — no hardcoded secrets
- **Operational discipline** — PR policies, approver guidance, and understanding the full deployment blast radius

The patterns described here are running in production at enterprise scale. The most important lesson: `fabric-cicd` performs a **full deployment every time** — treat every merge to a protected branch as a production deployment event and build your review and approval process accordingly.

---

## Resources

- [fabric-cicd GitHub](https://github.com/microsoft/fabric-cicd)
- [fabric-cicd PyPI](https://pypi.org/project/fabric-cicd/)
- [Microsoft Fabric Variable Library Docs](https://learn.microsoft.com/en-us/fabric/cicd/variable-library/variable-library-overview)
---

## About the Author

**Ajit Kumar Singh** is a Data Architect specializing in Microsoft Fabric and Power BI. He focuses on building scalable, enterprise-grade data platforms with a strong emphasis on CI/CD, deployment automation, and modern data engineering practices on the Microsoft stack.

Connect with Ajit on [LinkedIn](https://www.linkedin.com/in/ajit-kumar-singh-92847570)
