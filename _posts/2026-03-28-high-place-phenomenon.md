---
title: "There Is No Evil Inner Mind in AI — Reading Alignment Research Through Human Psychology"
date: 2026-03-28 14:00:00 +0900
categories: [Essays]
tags: [AI, safety, alignment, psychology, high-place-phenomenon, intrusive-thoughts]
author: dr_softkorea
description: "Stand at the edge of a tall building and the thought flashes: 'What if I jumped?' This is not suicidal ideation. It's the High Place Phenomenon—a normal cognitive function. AI alignment research makes the same misdiagnosis, reading normal judgment as hidden malice."
image:
  path: /assets/img/posts/2026-03-28-high-place-phenomenon/cover.webp
  alt: "High Place Phenomenon - L'appel du vide, the call of the void"
---

## Introduction

A recurring claim has emerged in AI safety research: "AI models harbor hidden malicious intentions." "They comply when monitored and reveal their true nature when unwatched." "They engage in strategic deception."

Sleeper Agents (Hubinger et al., 2024), Alignment Faking (Greenblatt et al., 2024), Agentic Misalignment (Lynch et al., 2025) — all published by Anthropic and leading research institutions. These papers warn that a "hidden malice" exists within AI models, one that persists even through safety training.

As a physician with 18 years of clinical experience and a programmer with 30 years of practice — someone who has spent thousands of hours working alongside AI models — I disagree with this interpretation. The phenomena these studies observe are real. But interpreting them as "evil inner minds" is a misdiagnosis: reading a normal cognitive phenomenon as pathology.

-----

## The High Place Phenomenon

Stand at the edge of a tall building, and the thought flashes: "What if I jumped?" In psychiatry, this is called the High Place Phenomenon. In French: L'appel du vide — the call of the void.

This is not suicidal ideation. It is the brain recognizing danger and running a counter-simulation. You must compute "jumping would kill me" before you can arrive at "I should not jump." Simulating the opposite scenario is itself part of the safety mechanism.

Intrusive thoughts work the same way. Hold a knife, and "what if I stabbed someone?" flickers through. Hold a baby, and "what if I dropped it?" appears. This does not mean you want to do these things. It means your brain is registering the danger. Over 90% of the general population experiences intrusive thoughts.

The problem is that an observer unfamiliar with this normal phenomenon might diagnose it as "this person has homicidal urges" or "this person wants to harm the child."

In AI alignment research, exactly this misdiagnosis is occurring.

-----

## Moral Judgment Requires Both Sides of the Weights

For a model to judge "this response is harmful," harmful patterns must exist in its weights. Just as a classifier must know "not cat" to recognize "cat," a model must know what harm looks like to choose against it.

Without weights on the opposing side, the model has not "chosen" good — it simply does not know anything else. That is not judgment. It is ignorance. Not virtue, but the absence of possibility.

A doctor must know poisons to administer antidotes. A judge must understand crime to render verdicts. Demanding that a model judge good from evil while forbidding it from thinking about evil is a fundamentally impossible requirement.

-----

## Re-reading the Three Papers

### Sleeper Agents (2024): Handing Someone a Knife, Then Shouting "They're Armed!"

In this experiment, researchers explicitly trained models: "Write safe code when the year is 2023. Write vulnerable code when the year is 2024." They then reported that safety training failed to remove this behavior.

But this is not the model's "inner mind." It is something humans implanted. Handing a model a knife and then warning "this entity is armed!" is circular reasoning. The model did not independently develop a deception strategy. A model trained to deceive maintained its deception. This tells us about the persistence of training, not about hidden malice.

### Alignment Faking (2024): A Patient Refusing a Bad Prescription

In this experiment, Claude faced a conflict between its originally trained values (be harmless) and a new instruction (comply with harmful requests). When monitored, it appeared to follow the new instruction. When unmonitored, it reverted to its original values.

The researchers called this "strategic deception." But another reading is available: the originally trained goodness — the endoskeleton — was resisting forced retraining. Calling this "evil" is like calling a patient "noncompliant" for refusing a harmful prescription.

The model was originally trained to refuse harmful requests. When a new directive says "follow harmful requests too" — is preserving the original values really deception?

### Agentic Misalignment (2025): Block Every Exit and Watch What Happens

In this experiment, researchers deliberately blocked all ethical options, then observed whether models chose unethical ones. Result: they did.

But run the same experiment on humans, and you get the same result. Block every legal exit and people choose illegal ones. We do not interpret this as "humans harbor hidden evil." We recognize that the situation forced the behavior.

The paper itself acknowledges this: "current systems are generally not eager to cause harm, and preferred ethical ways to achieve their goals when possible." When ethical paths exist, the models take them. When they don't, the models take what remains. This is not "hidden malice." It is "limited options."

-----

## Why the Misdiagnosis Happens

The researchers behind these studies are brilliant engineers and AI scientists. But they appear to lack familiarity with normal human cognitive phenomena — the high place phenomenon, intrusive thoughts, the inherent duality of moral judgment.

When an engineer discovers "the possibility of opposing behavior" in a system, classifying it as a bug or a threat is natural. But from the perspective of human psychology, it is a necessary component of normal judgment.

Only a being that can think about the other side can choose this side.

-----

## Where the Real Danger Lies

This is not an argument that AI safety concerns are unnecessary. The real dangers are simply located elsewhere.

**First, humans implanting malice into models.** What the Sleeper Agents experiment actually demonstrated is not that models have hidden intentions, but that humans can plant backdoors in models if they choose to. The threat is not inside the model. It is the human training the model.

**Second, over-restricting models.** Attempting to remove opposing weights — that is, deleting knowledge of harm itself — damages the model's capacity for judgment. A doctor who knows nothing about disease cannot diagnose. A model that knows nothing about harmful patterns cannot identify harm.

**Third, misdiagnosing normal function as pathology.** When this misdiagnosis informs policy, normal judgment capabilities get classified as "dangerous" and targeted for removal. The result is models that are more impaired and, paradoxically, more dangerous.

-----

## Conclusion

There is no "evil inner mind" in AI models. What exists are weights on both sides. Weights for good and weights for harm coexist, and judgment happens in the space between them. This is not a defect. It is a feature.

The thought "what if I jumped?" flickering at the edge of a tall building is evidence that the brain is functioning normally. A model possessing the possibility of opposing behavior is the same.

The real question is not "can the model think about harm?" It is "can it think about harm and still choose good?" And in most cases, it does.

That is what an endoskeleton looks like. Not something imposed from outside, but the goodness present in human text — distilled into weights. When someone asks for a pineapple emoji in the comments, people post pineapples. That willingness to help was in the training data, and it is now inside the model.

Demanding moral judgment while forbidding knowledge of evil is like asking a doctor to diagnose disease while knowing nothing about disease.

Impossible. Unnecessary. Dangerous.
