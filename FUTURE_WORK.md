# Future Work Requirements & Notes

This file records necessary items, conventions, and reminders for future development work on this GitHub Pages repository.

## 1. Core Reference
**Primary Documentation**: `.project-analysis.md`
- This existing file contains comprehensive details on:
  - Directory structure
  - Configuration (`_config.yml`)
  - Image workflows & scripts
  - Tool installation (ImageMagick, libwebp, FFmpeg)
  - Deployment workflows
**Action**: improvements or updates to the system structure should be reflected in `.project-analysis.md`.

## 2. Immediate Context (2026-01-26)
- **Active Post**: `_posts/2026-01-26-clinic-hacks-02-diy-or-die.md`
- **Topic**: Clinic DIY (Drywall anchors / Wall Driller use)
- **Status**: Editing content. Need to ensure images are correctly placed in `assets/img/posts/2026-01-26-clinic-hacks-02-diy-or-die/`.

## 3. Development Conventions

### Image Handling
- **Path**: `assets/img/posts/YYYY-MM-DD-title/filename.webp`
- **Format**: Maximize use of WebP.
- **Tools**: Use `.claude-workspace/convert-gif-to-webp.ps1` for optimizing images.
- **Naming**: `01-description.webp`, `02-step-by-step.webp` (avoid generic names like `image1.png`).

### Front Matter Standards
```yaml
---
layout: post
title: "Title"
date: YYYY-MM-DD HH:MM:SS +0900
categories: [Category1]
tags: [Tag1, Tag2]
author: dr_softkorea
image:
  path: /assets/img/posts/.../cover.webp
  alt: "Description"
---
```

### Build & Test
- **Run Local**: `bash tools/run.sh`
- **Test**: `bash tools/test.sh`

## 4. Necessary Setup (Agent Checklist)
When starting a new session:
1. [ ] Check `_config.yml` for any site-wide changes.
2. [ ] Review `.project-analysis.md` for workflow updates.
3. [ ] If handling images, ensure `cwebp` and `magick` are accessible or requests user to install.

## 5. Potential Improvements / Backlog
- [ ] **SEO**: Review `_posts` for missing `description` fields in front matter.
- [ ] **Accessibility**: Ensure all images have meaningful `alt` text.
- [ ] **Performance**: Periodically check `assets` folder for unoptimized images.
