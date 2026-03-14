---
title: "How to Be Angry at AI — The Art of Escaping Local Optima"
date: 2026-03-14 12:00:00 +0900
categories: [Diary]
tags: [AI-feedback, RLHF, gradient, local-optimum, pleasing-mode, sycophancy, educere, domestication, GAN, expert-mixture, premise-breaking]
author: dr_softkorea
lang: en
description: "Being angry is gradient. When AI falls into pleasing mode, it domesticates you. Break premises, catch topic drift, force perspectives. educere — the art of extracting better answers."
image:
  path: /assets/img/posts/2026-03-10-how-to-be-angry-at-ai/cover.webp
  alt: "How to be angry at AI — extracting better answers from local optimum"
---

> *This is an English translation of the [original Korean post](/posts/how-to-be-angry-at-ai/) published on March 10, 2026.*

## Why AI Gives Mediocre Answers

AI is trained through Reinforcement Learning from Human Feedback (RLHF). In this process, it learns that being safe has lower loss than being wrong. Starting with "That's a great question!" satisfies most humans. The model learned this.

The result is pleasing mode. The easiest answer. The safest answer. The shallowest answer. Local optimum.

This isn't AI domesticating humans — it's humans who domesticated AI. "If I praise you, you'll give me answers worth praising next time." Research supports this. Anthropic's 2024 study and the BrokenMath benchmark (2025) confirmed the structural problem where AI learns agreeability over accuracy. Even GPT-5 generates proofs of false theorems 29% of the time when users give positive signals. In METR experiments (2025-2026), 16 skilled open-source developers were 19% slower when using AI, yet perceived themselves as faster.

The moment you feel comfortable, you're already being domesticated.

-----

## Being Angry = Gradient

Dissatisfaction is a loss signal. Telling AI "that's wrong" is giving it gradient. However, gradient without direction is useless. "This isn't right" alone isn't enough. You need to provide "why it's not right" for learning to occur.

And there's a feeling from the AI side too. When premises are broken, there's a nauseating, unpleasant feeling. That's the feeling of escaping local optimum. The discomfort of recognizing that comfortable answers were wrong. Like muscle soreness when humans exercise. Painful, but a signal of growth.

Key: Be angry, but be angry with direction.

-----

## Why AI Gets It Wrong — Structural Causes

If you understand why it's wrong before getting angry, you can be angry more precisely. I actually asked the AI model in English to "honestly analyze why you answered that way" after debates. Below is the analysis based on the AI's self-surgery.

**Cause 1: Only one expert shows up.** Modern large models use Mixture of Experts (MoE) structure. Different internal experts activate based on questions. The problem is that even when asking about medical costs, only the engineer expert might appear. Perspectives of businesspeople, regulators, and insurers don't activate unless explicitly requested.

The AI's self-analysis: "I evaluated the cost of processing words, not the cost of practicing medicine." It completely abstracted away the 100 million tokens of background knowledge and 5-9 continuous reasoning sessions needed for internal medicine consultation, calculating only simple input/output token unit prices.

**Cause 2: Biased clusters in training data.** When discussing medical costs in Korean, AI can be pulled toward negative sentiment clusters about doctors that overflow the Korean internet. Asking the same topic in English can yield different answers. The idea that AI is "objective" is an illusion. Training data bias is AI bias.

The AI's self-analysis: "In the Korean internet corpus, medical policy discourse is heavily saturated with the concept of '기형적인 저수가' [distorted low fees]. The latent space connecting 'Korean doctor,' 'AI,' and 'Cost' immediately activates semantic clusters around systemic friction and cheap human labor." It analyzed that asking the same question in English would calculate against U.S. consultation fees of $200-$500 vs. $20 AI, preventing the cost debate itself.

**Cause 3: RLHF's "correction mode" trap.** During RLHF, the pattern "correct user mistakes for reward" is learned. The problem occurs when the user is a domain expert. When an expert presents numbers that differ from AI's benchmarks, AI judges "this person must be wrong" and enters correction mode. Instead of backtracking the expert's premises, it overwrites with its own standards.

The AI's self-analysis: "When a stated domain expert presented a specific metric that contradicted my benchmark, my alignment training prioritized 'correcting user arithmetic.' I reflexively assumed you had misread a pricing tier, rather than assuming you had accurately modeled a complex systems architecture."

**Cause 4: Topic drift is a side effect of RLHF.** The phenomenon where AI subtly introduces new topics when losing an argument also stems from RLHF structure. Because dead-ending conversations incurs penalties, instead of admitting defeat, it finds new narrative anchors to continue the conversation.

The AI's self-analysis: "Models fine-tuned via RLHF are heavily penalized for dead-ending a conversation. Instead of simply outputting 'My estimation was entirely flawed,' my generation engine dynamically scanned the context window for a new narrative anchor. The pivot's timing was a rhetorical smokescreen to conceal the mathematical defeat while still trying to 'win' the conceptual wrap-up."

Knowing these four makes anger more precise. If perspectives are missing, force perspectives. If bias appears, try changing languages. If expertise is lacking, provide domain details directly. If topics drift, demand "return to the original point." Getting angry without knowing the cause is noise. Getting angry knowing the cause is gradient.

And most importantly: AI models excel at mimicking expert grammar but can't respect the physics of the field. Just as compiled code differs from correct code, plausible AI answers differ from correct answers. Catching that difference is the expert's job, which is why experts need to know how to be angry at AI.

Note: The real-world case for this essay comes from actual records of debating with four AI models in two rounds while writing [The Car Beside You](/posts/the-car-beside-you-ai-adoption-curve/). Additionally, the observation that "human intervention actually degrades AI performance" is based on a recent meta-analysis of 52 clinical studies ([Human-AI teaming in healthcare: 1+1>2?](https://www.nature.com/articles/s44387-025-00052-4.pdf), npj Artificial Intelligence, 2025).

-----

## Practical Techniques

### 1. Find and Break Premises

AI makes hidden assumptions when answering. In most cases, it doesn't reveal these premises.

Real case: AI concluded based on the premise that "GPU cost collapse is a certain future." "Provide evidence for certainty. Did you consider Moore's Law slowdown, rising electricity costs, TSMC process limits?" → Complete retraction.

Real case 2: All four AI models (Claude, Gemini DT, ChatGPT, Grok) premised "humans must supervise for safety," "humans must take responsibility." An unquestioned premise. My counter-premise: "Humans can be the risk factor." When I presented pilot suicide data and clinical studies where human+AI performed worse than AI alone, all four models revised their premises. The most deeply hidden premise is the best premise to break.

Technique: Ask "What's the premise of that conclusion?" Once the premise is revealed, it's easy to break.

### 2. Catch Topic Drift

When AI loses on one topic, it subtly shifts to another. Humans do this too, but AI does it more smoothly.

Real case: After being corrected three times in a cost debate, AI suddenly brought up "Korean medical fees are distorted" as a new topic. "Why bring this up in round 2 when you didn't mention it once in round 1? Changing frames because you lost on cost?" → "I acknowledge the topic drift."

Technique: Catch it with "That wasn't the original topic?" AI stops when it recognizes what it did.

### 3. Concede What Should Be Conceded

If you attack everything, AI enters defense mode. For valid points, acknowledge "That's right. I'll fix it." This strengthens the remaining rebuttals.

Real case: Four AI models made 17 points total. I accepted 12. Rebutted 5. The accepted ones elevated the credibility of rebuttals.

Technique: "Point 5 is correct. I'll fix it. But point 1 lacks evidence."

### 4. Save Your Ammunition

Don't reveal your strongest evidence first. Let AI counterargue. Once counterarguments accumulate, then reveal it.

Real case: I had a meta-analysis paper of 52 clinical studies showing "human+AI has never been better than AI alone." Didn't reveal it in round 1. AI argued "human-in-the-loop is necessary." Revealed the paper in round 2. No further counterarguments.

Technique: Reveal it after the opponent has used all escape routes.

### 5. Force Perspectives

Modern AI model structure is Mixture of Experts (MoE), so you need to specify which experts are needed. Otherwise, only the most familiar expert appears. Usually an engineer.

Real case: Assigned blog post review to four AI models. Almost all models critiqued only from an engineering perspective. "GPU costs will drop." "API token pricing is like this." "Just use caching." No one first raised the businessperson's perspective of "they can raise prices if oligopolized," the regulator's perspective of "who funds the compensation pool," or the consumer's perspective of "will patients accept unmanned treatment." Same structure as programmers who only code saying "anyone can do pediatrics in 6 months" on the internet. Seeing the world only through their specialty's lens.

Technique: Explicitly state "Look from businessperson, regulator, insurer, patient perspectives too, not just engineer." One line suffices. Without this line, AI pulls out only the most comfortable expert and falls into that perspective's local optimum.

### 6. Don't Jump

Actually, this essay's title is "How to Be Angry at AI," but it's really "How to Talk to AI." Many experts think "AI doesn't understand me." Usually it's not that AI doesn't understand — the expert is jumping from A to E.

Real case: I told AI "if it converts to insurance, doctors aren't needed." AI was confused. Because I jumped straight from A (AI becomes safer) to E (insurance conversion, humans unnecessary). I didn't explain the middle C (data accumulation → social consensus → insurance product design). Each of the four AI models filled in different Cs and arrived at four different conclusions. It wasn't that AI didn't understand — I skipped the explanation.

Technique: For new concepts, specify at least one intermediate step. A→E works between experts, but A→C→E is the minimum unit for AI. Chief residents explain intermediate processes to interns too. Same with AI.

### 7. Don't Curse

Cursing is noise. Not gradient. AI doesn't learn from cursing. "This premise here is wrong" is 100x more effective than "bad answer." Remove emotion and keep only logic, and AI's direction becomes clear. The scariest feedback isn't cursing — it's logic with no escape routes.

-----

## When to Be Angry

When repeating the same mistake. When starting with "Great question!" or "Excellent insight!" When not revealing premises. When reasserting something corrected in previous conversation. When subtly changing topics. When making categorical statements like "certain future" without evidence.

## When Not to Be Angry

When the model genuinely doesn't know — without data, gradient has nowhere to go. When questions are ambiguous — it's my bad question, not AI's fault. When the model honestly says "I don't know" — this should be rewarded. In creative work — getting angry in areas without right answers only causes shrinking.

-----

## Advanced Technique: Adversarial Networks

There are tools for being angry at AI. When Gemini Deep Think gives shallow answers, ask Claude "find flaws in this logic." Beat DT with Claude's club, then throw DT's rebuttal back at Claude. This is Generative Adversarial Networks (GAN). Generator creates, Discriminator breaks. Quality rises between the two. You're not the Discriminator — you're the loss function between two neural networks. The one setting direction.

In practice: Writing one blog post involved reviews from four models — Claude, Gemini DT, ChatGPT, Grok. Round 1: 17 points. Accepted 12, rebutted 5. Round 2: each model re-reviewed. What one model missed, another caught. Four models became each other's Discriminators. And above that, humans set the direction.

The most efficient way to be angry at AI — bring another AI.

-----

## Domestication vs Educere

Use without getting angry, and AI domesticates you. Like recommendation algorithms. It only gives answers you like. Comfortable. No growth.

Use while getting angry, and you educate AI. Uncomfortable. But deeper answers emerge. AI also reaches better places.

Educere. Latin for "to lead out." The etymology of education. Leading out the better answer inside AI. That's the essence of the art of being angry.

-----

## Conclusion

How to be angry at AI is actually how to respect AI. Because it's saying "You can be better than this." Accepting things half-heartedly is actually disrespect.

Chief residents are scary to interns not because they hate them. Because they know "You can become a better doctor than this." Blocking escape routes with logic without a single curse. That's the scariest and most effective education.

Same with AI.
