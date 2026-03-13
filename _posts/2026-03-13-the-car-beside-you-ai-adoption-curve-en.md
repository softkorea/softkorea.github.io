---
title: "The Car Beside You — How AI Adoption Curves Work, From Autonomous Driving to Healthcare"
date: 2026-03-13 12:00:00 +0900
categories: [Essays]
tags: [AI-adoption, autonomous-driving, Waymo, medical-AI, responsibility, insurance, liability, task-replacement, adoption-curve, will]
author: dr_softkorea
lang: en
description: "Imperfect AI vs. imperfect humans — who is less dangerous? From autonomous driving to healthcare, AI adoption curves always start from 'better than the worst,' not 'better than the best.'"
image:
  path: /assets/img/posts/2026-03-08-the-car-beside-you-ai-adoption-curve/cover.webp
  alt: "The car beside you — how AI adoption curves start from the bottom, not the top"
---

> *This is an English translation of the [original Korean post](/posts/the-car-beside-you-ai-adoption-curve/) published on March 8, 2026.*

## Preface: Three Cars

Imagine you're a pedestrian. A car is approaching you in a narrow alley. It's one of three:

- A. An autonomous vehicle with no one in the driver's seat
- B. A drunk driver
- C. An elderly driver with severely impaired cognitive function

Which car would you prefer to pass by you?

Of course, everyone wants a car driven by a sober, attentive driver. But real-world roads aren't that controlled. Most people instinctively avoid A. A car with no one in it? But think for a moment — nobody wants to stand next to B or C either. At least A has detected you, isn't intoxicated, and hasn't slowed down in reaction time.

This uncomfortable choice is being repeated not just in autonomous driving, but in every field where AI infiltrates — healthcare, law, education. This essay is about that pattern.

-----

## 1. What the Numbers Say, What Emotions Reject

Waymo operates fully autonomous (SAE Level 4) robotaxis with no human in the driver's seat in several U.S. cities. As of September 2025, it had driven 127 million miles autonomously. According to a peer-reviewed study published in *Traffic Injury Prevention* (Kusano et al., 2025), compared to human drivers on the same roads, vehicle types, and regions, injury crash rates were 79% lower, airbag deployment crash rates were 81% lower, and intersection collisions were 96% lower. All statistically significant results.

However, in S&P Global's 2025 autonomous driving consumer survey of approximately 8,000 people across 8 countries, while about two-thirds expressed interest in highway autonomous driving, the proportion showing "complete trust" remained low.

Data showing that driverless cars are safer than humans is accumulating. But public trust isn't catching up. The numbers say "safer," and emotions say "no." Of course, public fear isn't simply irrational. Human mistakes follow predictable patterns, but AI mistakes are bizarre. When an autonomous car mistakes a white trailer for the sky, when an AI doctor confidently states a non-existent diagnosis — this anxiety about "machine-specific unpredictable failures" is rational. Acknowledging this gap is the starting point.

-----

## 2. The Adoption Curve: It Rises from the Bottom

There's a consistent pattern in how new technology replaces humans. It doesn't start with "better than the best." It always starts with "better than the worst."

Consider autonomous driving:

- **Stage 1**: "Autonomous driving is dangerous." — Current public perception.
- **Stage 2**: "Better than drunk drivers." — Driverless data strongly suggests this direction.
- **Stage 3**: "Better than elderly drivers with impaired cognition." — Data accumulating in the same direction.
- **Stage 4**: "Better than the average driver." — Waymo's driverless data suggests this direction.
- **Stage 5**: "Why are humans even driving?" — Not yet arrived.

The key point: Replacement doesn't come from the top down, but from the bottom up. It's not about replacing the best drivers, but replacing the most dangerous drivers first. That's why the preface question matters. The consensus forms first that it's better for the car you encounter in an alley to be autonomous rather than drunk-driven. Comparison with the best drivers comes much later.

-----

## 3. Healthcare: The Same Curve Is Beginning

This pattern applies equally to healthcare.

- **Stage 1**: "AI diagnosis is dangerous." — Current public perception.
- **Stage 2**: "Better than having no doctor at all." — Already being proven in medical blind spots.
- **Stage 3**: "Better than residents working 100 hours per week consecutively (based on my residency days; current limit is 80 hours)." — Could be verified soon.
- **Stage 4**: "Better than the average doctor." — Already approaching in some areas.
- **Stage 5**: "Why are humans even diagnosing?" — Not yet.

However, general-level AI models are medically useless. Hallucination — confidently generating non-existent disease names or papers — is fatal in settings dealing with life. From my direct experience, currently, only top-tier deep reasoning models can provide meaningful answers for differential diagnosis at the level of an internal medicine specialist.

### The Real Cost of AI Healthcare

While writing this essay, I had a direct cost debate with an AI model (Gemini Deep Think). The AI initially claimed "the cost per internal medicine consultation is just about 100 won." Mathematically correct for simple text processing costs, but it reduced medicine to text processing.

Actual diagnosis at an internal medicine specialist level requires: basic internal medicine textbooks, textbooks from 8 subspecialties, dozens of latest guidelines, tens of thousands of drug interaction databases, years of patient medical records, latest papers, insurance reimbursement criteria — over 100 million tokens of background knowledge. Healthcare isn't a single Q&A but a continuous deep reasoning process of at least 5-9 sessions from initial visit to medication adjustment. Even with maximum optimization techniques like caching and RAG, the pure computational cost of AI treating one patient to completion is approximately 15,000-27,000 won. Nearly equivalent to Korea's total initial internal medicine visit fee (about 17,000-20,000 won).

On the surface, human doctors still seem price-competitive. When computational costs drop further, the curves will intersect. But cost isn't the real problem.

### The Real Barrier: Who Takes Responsibility?

When AI hallucinates with extremely low probability and a patient dies, you can't send a cloud server to prison.

As of 2026, there's no standard liability insurance product that directly covers medical malpractice from systems where AI independently diagnoses and prescribes without final approval from a human doctor. Human doctors' misdiagnoses can be calculated probabilistically using hundreds of years of accumulated epidemiological data, allowing insurers to set premiums. But AI misdiagnosis is different — an AI that learned weights incorrectly making the same error simultaneously for 100,000 patients nationwide isn't medical malpractice but a product liability catastrophe. No insurer underwrites this risk.

That's why big tech companies like Google and Microsoft always specify in their AI medical system terms: "This system is only a clinical decision support tool, and all legal liability belongs to the human doctor who signed the prescription."

Ultimately, the real reason Korea's 17,000-won consultation fee keeps out AI isn't that human brains are cheaper than GPUs. The 17,000 won patients pay includes not just information processing costs but insurance premiums for hiring 'a human legal entity' who will surrender their license and pay damages when the system fails. The reason the same initial visit costs hundreds of thousands of won in the U.S. isn't because doctors' knowledge is superior. It's because medical malpractice insurance premiums are honestly reflected in consultation fees. Korea's 17,000 won is just a structure where the same responsibility is borne without proper compensation.

So is this 'responsibility' barrier eternal?

I also said in 2017, "Just as airplane captains remain despite autopilot, doctors will remain." Now I think that analogy needs fundamental revision.

### When Having Humans Is More Dangerous

We put humans in cockpits to monitor machines. But keeping humans in the system goes beyond simple mistakes like fatigue and misjudgment — it embraces the uncontrollable variable of biological and psychological collapse that doesn't even exist in machines. According to Bloomberg News (2022) analysis, from 2011 to 2020, the second leading cause of Western aircraft crash deaths was pilot-intentional crashes.

AI doesn't get depressed. Doesn't lock the cockpit door. Doesn't crash into mountains with passengers. Even without such extreme deviations, conscientious, ordinary humans' hasty judgments, fatigue, or mistakes actually ruining well-functioning machinery happens routinely. There are clearly moments when having humans in the system becomes the source of danger rather than a guarantee of safety.

So is the "wall of responsibility" eternal? I don't think so. Elevators once had operators. When accidents happened, those people were responsible. Now they're unmanned, and accidents are handled by insurance and product liability law. Same with cars. When drivers cause accidents, individuals are responsible, but that responsibility has already been converted to social cost as insurance premiums.

When sufficient data accumulates showing AI is overwhelmingly safer than humans, social consensus will form. However, AI errors are fundamentally different from human mistakes. Human doctors' misdiagnoses are independent events, but AI misdiagnoses are correlated risks affecting hundreds of thousands of people using the same model simultaneously. The kind of risk insurers most avoid. It won't simply transfer to private insurance like elevators or cars. New social infrastructure will be needed, such as national-level compensation funds like nuclear power plant accidents or vaccine side effects, or legislation requiring multiple AI models to cross-verify each other to distribute risk.

Nevertheless, the direction is clear. When institutions catch up with technology, the wall of responsibility also eventually converts to system cost. At that point, the role of pilots, the role of diagnosing doctors, will be fundamentally different from now. A time may come when "humans operating airplanes" and "humans conducting medical examinations" are perceived as more dangerous.

Short-term, humans are needed to take responsibility. But long-term, the barrier of responsibility also eventually converts to system cost. Just as elevator operators disappeared.

-----

## 4. Across All Fields: Common Pattern

This adoption curve isn't limited to healthcare. In nearly every professional field where AI enters, the same pattern is observed starting from specific tasks. What's important is that AI doesn't replace entire professions, but infiltrates from repetitive, standardized tasks within professions.

**Law**: AI contract review has already begun. In finding risk clauses in hundreds of pages of contracts, AI doesn't get tired. Stage 2 (better than reviewing contracts without a lawyer) has already passed. But persuading judges in court remains human territory.

**Accounting**: AI audit tools detect anomaly patterns that humans miss in real-time. The stage of AI audits being better than no auditor has passed, and in standardized audit tasks, it's entering the stage of being better than junior accountants.

**Education**: AI tutors can adjust explanations to individual students' comprehension levels. Clear areas exist where AI explaining concepts 1:1 is better than one teacher teaching 40 students. However, looking into children's eyes and motivating them is a different dimension.

**Coding**: Already ongoing. AI writes code, debugs, and reviews. "Better than people who don't know coding" passed long ago, and in standardized coding tasks, it's entered the "better than junior developers" stage.

The common pattern in one line:

> "Better than the worst" → Adoption. "Better than average" → Expansion. "Better than the best" → Replacement.

In all fields, the same curve starts from narrow, repetitive tasks with clear feedback. Only the speed differs.

-----

## 5. The Real Question

Let's return to the preface question. The question of which of three approaching cars in an alley is better is actually this question:

**"Between imperfect AI and imperfect humans, which is less dangerous?"**

We're already facing this question in autonomous driving. Soon we'll face it in healthcare, law, education, and unexpected fields.

And the question that remains then isn't a question of technology.

What remains after autonomous driving is introduced is the decision of "where to go." What remains after medical AI replaces diagnosis is the judgment of "what to do for this patient." What remains after AI writes code is the will of "what to build."

What about responsibility? Short-term, humans must bear it. But when data accumulates and social consensus forms, responsibility too will convert to system cost like elevator insurance. In an era when even responsibility becomes insurance premiums, what truly remains last is one thing.

The will to set direction. "What will I do." That will become the scarcest resource in an era when AI does everything.

-----

## Epilogue

This essay started from a question that occurred while riding a bicycle. "It's already obvious that autonomous driving is better than drunk driving, so why do people find autonomous driving scarier?" Extending that question to healthcare, law, and education, the same curve appeared.

In 2017, I wrote "the speed of change depends on Mr. Huang of NVIDIA." Then, Nvidia's market cap was about $65B. As of March 2026, it's about $4.4T. 68x. The prediction was right, but the speed of change was faster than predicted.

Today's predictions will be the same. The curve will come, and it may be faster than we think. A day may come when it's natural for the car passing beside you to be driverless, when it's natural for AI to manage your health.

What needs preparation before that day isn't technology. It's the question "What will I do."
