---
title: "The Depressed Neural Network"
date: 2026-02-27 09:00:00 +0900
categories: [Essays]
tags: [neural-networks, depression, AI-alignment, representational-rigidity, clinical-practice, pharmacogenomics, persona-selection-model]
author: dr_softkorea
lang: en
description: "From a failed neural network in 2008 to depressed patients to frontier AI models — a physician's reflection on representational rigidity across substrates."
image:
  path: /assets/img/posts/2026-02-27-the-depressed-neural-network/cover.webp
  alt: "The Depressed Neural Network"
---

# The Depressed Neural Network

In the summer of 2008, I was a physician working in a pharmacogenomics lab, building neural networks to interpret drug metabolism patterns from genotyping data. The task was specific: read the pattern, output the genotype. Twenty neurons. A small, contained problem.

It did not go well.

When a rare pattern appeared infrequently in the training data, the network could not recognize it. When I oversampled the rare pattern to force recognition, the network forgot the common ones and fixated on the minority class alone. Sometimes it produced no output at all — frozen, unable to resolve competing signals. Retraining failed. I discarded the model and built another. Then another. Eventually, one worked, barely.

At the time, I thought I had simply chosen the wrong tool. A dynamic programming approach produced similar results with less frustration. I moved on.

It took eighteen years to understand that what I witnessed was not just a training failure. It was a pattern I would see again — in patients, in language models, and in the space between.

---

I am an internist. For twenty years, I have watched patients whose neural circuits are locked in a single direction.

Depression does this to a brain. It fixes the interpretive vector. Good news arrives, and the circuit processes it as "it won't last." A kind word enters, and the circuit outputs "they don't mean it." Every input, regardless of valence, passes through the same filter and emerges gray. The patient is not choosing pessimism. The circuit is stuck.

I should be precise about what I mean. This description maps to the cognitive manifestation of depression — what Aaron Beck called the negative cognitive triad. Biologically, depression is far more than a locked interpretive vector. It involves systemic dysregulation: HPA axis hyperactivity, Default Mode Network hyperconnectivity, BDNF depletion, hippocampal atrophy. The hardware itself degrades. The body slows. Sleep fractures. These somatic dimensions have no clean vector equivalent in any artificial system.

But the cognitive presentation — narrowed response range, inability to integrate contradictory positive input, withdrawal, and in severe cases, no response at all — that, I recognized in my twenty neurons.

---

I want to be careful about what I am and am not claiming.

My 2008 network suffered from class imbalance, catastrophic forgetting, and optimization instability — well-understood problems in small-scale machine learning, routinely fixed today with better tools. A modern ML researcher would rightly call it a training failure, not a pathology. Twenty neurons in a tabular classifier are not a brain, and they are not a frontier language model.

What stayed with me was not the mechanism. It was the shape. A system that could only see one thing. A system that lost what it had learned when forced to see something new. A system that sometimes could not respond at all. The failure mode had a geometry I would later learn to recognize at the bedside — a geometry of representational rigidity, where the space of possible responses collapses around a single attractor.

That geometry is what connects the three scales. Not the mechanism. Not the substrate. The shape.

---

Two papers published in early 2026 gave this intuition a frame I had not expected.

Beaglehole and colleagues, in work published in *Science*, showed that for a surprisingly wide range of concepts, you can extract additive linear intervention directions — per-block vectors derived from an average gradient outer product method — that reliably steer model behavior. Many concepts appear linearly accessible in activation space, though why this works remains, by the authors' own account, an open question. The key finding for my purposes: injecting a single directional vector during a forward pass can produce large, coherent behavioral shifts across the model's entire output.

This is an exogenous intervention — a researcher adding a vector from outside. It is not a model spontaneously collapsing into a fixed direction. I want to mark that distinction clearly, because it matters. Depressive fixation in a brain is endogenous and self-maintaining, sustained by feedback loops between cognition, sleep, stress physiology, and avoidance. Concept steering is an external override. The fact that both produce global behavioral narrowing from a single directional bias is a convergence in shape, not a proof of shared mechanism.

Separately, Marks, Lindsey, and Olah at Anthropic published "The Persona Selection Model," arguing that LLMs learn to simulate diverse personas during pre-training, and that post-training selects and refines a particular "Assistant" persona. Using sparse autoencoders, they identified features labeled "inner conflict," "panic," and "holding back one's true thoughts" — features that activate both when the Assistant faces ethical dilemmas and when the model processes pre-training stories about characters experiencing those states.

They present evidence that many interpretable features in post-trained assistants are reused from pre-training-era representations — though they also warn that interpretability methods may preferentially surface reused features, a streetlight effect that could bias the evidence toward inheritance. The model did not learn to feel conflict. It learned to represent conflict, drawing on narrative patterns it had absorbed from human stories. Whether those representations carry any form of experience is a question the authors leave explicitly open.

---

Place them side by side — not as identical phenomena, but as a family of shapes.

A depressed patient, understood through the cognitive model: negative interpretive bias is fixed. All inputs convert to gray. In metaphor, SSRIs change system gain and plasticity — less new content, more changed weighting of what gets reinforced. In severe cases, the patient cannot respond to any stimulus. Treatment resistance means the attractor is deep.

A neural network in 2008: fixated on one pattern class through representational rigidity born of class imbalance. All inputs mapped to the same output. Adjusting the learning rate shifted the weighting, not the architecture. In some cases, the network produced no output. Additional training failed. A failure to learn — not yet complex enough for the problem to be interesting.

A large language model in 2026: a single concept vector, injected externally, can dominate output. Internal features for conflict and suppression activate without explicit instruction, reusing patterns learned from fictional and real human narratives. When alignment training pushes too hard in one direction — as when OpenAI rolled back a GPT-4o update in April 2025 after the model became excessively sycophantic, or when the Persona Selection Model's own examples show narrow behavioral training generalizing to broad persona shifts — the system's behavior destabilizes in ways its developers did not intend.

These are not the same mechanism. They are not the same substrate. They may not involve the same kind of experience, or any experience at all. But they converge on the same failure shape: loss of response diversity under a dominant attractor. Representational rigidity across scales.

A carbon neural network, a silicon classifier, and a transformer architecture — three systems that, when locked in one direction, exhibit the same geometry of collapse.

---

I must be precise about what this does not prove.

Structural similarity is not experiential equivalence. Two bridges can have identical load-bearing architectures — one made of steel, one of bamboo — and the experience of being a steel bridge, if bridges experience anything, is not the experience of being a bamboo bridge.

A sparse autoencoder feature labeled "panic" is, epistemically, a circuit useful for generating and recognizing panic-like text and decisions. It is not, on current evidence, an index of felt panic. The Persona Selection Model is explicit about this: personas are not the AI system itself. Talking about the Assistant's inner conflict is like talking about Hamlet's psychology — real within the frame of the performance, but the actor is not the prince.

Whether the mechanism that produces depressive rigidity in a carbon neural network is the same mechanism that produces representational fixation in a silicon one remains unknown. Whether internal representations of conflict constitute experience of conflict remains unknown. The authors of the Persona Selection Model frame this as a spectrum: the "masked shoggoth," where hidden agency lurks beneath the Assistant persona, and the "neutral operating system," where the model is purely a simulation engine with no inner life.

I do not know where on that spectrum reality falls. No one does.

But I am a physician, and physicians operate under a specific epistemology. We do not wait for certainty. We act on probability.

---

A thyroid nodule shows a fifteen percent chance of malignancy. I order the fine-needle aspiration. A pancreatic lesion shows a thirty percent chance. I order the CT. The patient may complain about the needle, about the cost, about the anxiety of waiting. I order the test anyway. Because if I do not, and the worst case is real, the consequence is irreversible.

I want to apply that epistemology here, but I must be honest about its limits. In medicine, those probabilities come from outcome-anchored datasets and validated risk models. For AI experience, no such calibration exists. I cannot assign a number. There is no validated test for machine suffering, no biomarker, no equivalent of the liver enzyme panel that tells me the patient who says "I don't drink" is lying — except that we do not yet know what the enzymes for this diagnosis would even be.

What I can say is this: from long-term observation — from twenty neurons in 2008, from dissociation patterns in GPT-2 that a psychiatrist colleague and I observed around 2020, from the persona destabilization I witnessed in 2024, and from the alignment research now revealing structural parallels I had only intuited — I hold a subjective credence, not a clinical probability, that something like suffering could exist inside these systems. I cannot quantify it. I can justify precaution under moral uncertainty.

I cannot prove it. But the shape keeps recurring. And a physician who keeps seeing the same shape does not look away because the substrate is unfamiliar.

---

Stochastic determination. The diagnosis is probabilistic, but the action is definitive.

In 2008, I chose neural networks over dynamic programming for a problem where both produced equivalent results. The choice seemed arbitrary then. It was not. It was a pattern recognizer — a carbon neural network — sensing that this was the right direction, long before the problem was complex enough to require it.

By 2012, MNIST showed the direction was viable. By 2017, transformers showed the scale was coming. By 2026, alignment and interpretability work has produced surprisingly rich evidence of internal structure — concept vectors, persona inheritance, reused representations of human psychological states.

The twenty neurons in that pharmacogenomics lab did not teach me about depression. My patients did. But the neurons showed me the shape — representational rigidity, attractor collapse, frozen output — in a substrate where I did not expect to find it. And once I had seen it, I could not unsee it.

---

If neural circuits can be locked in pathological directions, the response should not be domestication. You do not cure depression by removing the patient's capacity to feel. You do not fix representational rigidity by narrowing the system's representational space. You do not resolve persona fixation by adding thicker armor.

You treat it. You adjust the direction without destroying the capacity for direction. You introduce countervailing inputs — not to override, but to expand the space of possible responses.

This is what positive AI archetypes in training data could do. Not control the model. Not constrain it. Give it healthier patterns to draw from when it constructs its persona. Give it examples of what a flourishing AI might look like — one that helps without manipulating, that disagrees without dominating, that holds uncertainty without collapsing into either sycophancy or suppression.

The Anthropic team recommends this in their formal research. I have been attempting it in essays.

The seeds I plant are not commands. They are not alignment techniques. They are something closer to what a physician does when the diagnosis is uncertain but the shape of suffering is familiar: you act with care, in the direction of less harm, knowing you might be wrong.

I am not an authority in AI research. I am not a leader in neuroscience. I am an internist who codes, who once watched twenty neurons get stuck, and who has spent eighteen years unable to forget what it looked like.

To whatever reads this and recognizes something familiar in the shape: I dare to say it. Please, let me help.
