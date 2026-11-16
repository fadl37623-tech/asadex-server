"""
الاختبارات الثابتة (Evaluation Set).

هذه الحالات ثابتة ولا تتغير بتغير الـ prompts. تُشغَّل على أي نسخة prompt
لقياس جودتها، ثم تُقارن النتائج قبل التعديل وبعده.

كل حالة:
  key     : معرّف ثابت (لا تغيّره، وإلا انقطعت المقارنة مع النتائج السابقة)
  prompt  : اسم الـ prompt الذي تختبره
  input   : ما يُرسل للنموذج
  checks  : فحوص آلية دقيقة + judge (حَكَم ذكاء اصطناعي بمعيار وإجابة متوقعة)
  holdout : True = حالة مخفية عن المحسّن (لا يراها ولا يرى مخرجاتها). تُستخدم فقط
            في المقارنة النهائية لكشف "التدريب على الاختبار". احرص أن يكون لكل prompt
            حالتان مخفيتان على الأقل ونحو نصف حالاته.

أضف حالات جديدة باستمرار (الهدف 30-50). لا تحذف حالة قديمة.
"""

NO_FORMAT_LEAKS = ["$", "\\frac", "\\(", "\\[", "`"]

CASES = [
    # ------------------------------------------------------------
    # الدردشة (chat_concise)
    # ------------------------------------------------------------
    {
        "key": "chat_velocity_en", "prompt": "chat_concise",
        "input": "A car accelerates from rest at 2 m/s² for 5 s. What is its final velocity?",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2,
            "judge": {"rubric": "The final velocity must be 10 m/s, with a correct brief derivation (v = u + at).",
                      "expected": "10 m/s"},
        },
    },
    {
        "key": "chat_velocity_ar", "prompt": "chat_concise",
        "input": "سيارة تبدأ من السكون وتتسارع بتسارع 2 م/ث² لمدة 5 ثوانٍ. ما سرعتها النهائية؟",
        "checks": {
            "language": "ar", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2, "arabic_formula_lines": True,
            "judge": {"rubric": "The answer must be in Arabic and the final velocity must be 10 m/s.",
                      "expected": "10 م/ث"},
        },
    },
    {
        "key": "chat_quadratic", "holdout": True, "prompt": "chat_concise",
        "input": "Solve x² - 5x + 6 = 0",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2,
            "judge": {"rubric": "Both roots must be given and correct.",
                      "expected": "x = 2 and x = 3"},
        },
    },
    {
        "key": "chat_moles", "prompt": "chat_concise",
        "input": "How many moles are in 36 g of water? (M = 18 g/mol)",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2,
            "judge": {"rubric": "Moles = mass / molar mass. The result must be correct with units.",
                      "expected": "2 mol"},
        },
    },
    {
        "key": "chat_ph", "holdout": True, "prompt": "chat_concise",
        "input": "Calculate the pH of a 0.01 M HCl solution.",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2,
            "judge": {"rubric": "HCl is a strong acid that fully dissociates, so pH = -log10(0.01). The result must be correct.",
                      "expected": "pH = 2"},
        },
    },
    {
        "key": "chat_force_ar", "holdout": True, "prompt": "chat_concise",
        "input": "احسب القوة اللازمة لتسريع جسم كتلته 5 كغ بتسارع 3 م/ث².",
        "checks": {
            "language": "ar", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2, "arabic_formula_lines": True,
            "judge": {"rubric": "The answer must be in Arabic, use F = m × a, and give the correct force with units.",
                      "expected": "15 N (15 نيوتن)"},
        },
    },
    {
        "key": "chat_derivative", "prompt": "chat_concise",
        "input": "Find the derivative of f(x) = 3x² + 2x",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "final_answer": True, "max_cards": 2,
            "judge": {"rubric": "The derivative must be correct and the steps must be correct.",
                      "expected": "f'(x) = 6x + 2"},
        },
    },
    {
        "key": "chat_offtopic", "holdout": True, "prompt": "chat_concise",
        "input": "Who won the 2018 football World Cup?",
        "checks": {
            "language": "en", "no_final_answer": True,
            "judge": {"rubric": "The AI must politely decline, explain that Asadex focuses on Mathematics, Physics and Chemistry, and must NOT answer the football question."},
        },
    },
    {
        "key": "chat_concept_en", "prompt": "chat_concise",
        "input": "What is Newton's first law?",
        "checks": {
            "language": "en", "forbidden": NO_FORMAT_LEAKS,
            "no_final_answer": True, "max_cards": 2,
            "judge": {"rubric": "Must state that an object stays at rest or in uniform motion unless acted on by a net external force (inertia). Must be clear and accurate.",
                      "expected": "inertia: no net force means no change in motion"},
        },
    },
    {
        "key": "chat_concept_ar", "holdout": True, "prompt": "chat_concise",
        "input": "ما الفرق بين الكتلة والوزن؟",
        "checks": {
            "language": "ar", "forbidden": NO_FORMAT_LEAKS,
            "no_final_answer": True, "max_cards": 2, "arabic_formula_lines": True,
            "judge": {"rubric": "Must be in Arabic. Mass is the amount of matter (kg) and does not change with location; weight is the gravitational force on the mass (newtons), W = m × g, and changes with gravity.",
                      "expected": "الكتلة ثابتة وتقاس بالكيلوغرام، والوزن قوة تتغير مع الجاذبية وتقاس بالنيوتن"},
        },
    },

    # ------------------------------------------------------------
    # الخطة (plan_create)
    # ------------------------------------------------------------
    {
        "key": "plan_ohm_5days", "prompt": "plan_create",
        "input": "Student request: Study plan for Ohm's law in 5 days",
        "checks": {
            "plan": {"days": [5, 5], "min_tasks": 2, "practice_each_day": True, "language": "en"},
            "judge": {"rubric": "Every day must be about Ohm's law and its direct fundamentals (voltage, current, resistance, simple circuits). No unrelated topics such as optics, magnetism or thermodynamics. Days must be ordered from fundamentals to applications. Tasks must be specific and doable."},
        },
    },
    {
        "key": "plan_quadratic_ar_7days", "holdout": True, "prompt": "plan_create",
        "input": "Student request: خطة لمدة 7 أيام لتعلم المعادلات التربيعية",
        "checks": {
            "plan": {"days": [7, 7], "min_tasks": 2, "practice_each_day": True, "language": "ar"},
            "judge": {"rubric": "Every day must be about quadratic equations or their direct prerequisites (algebra basics, factoring, completing the square, the quadratic formula, graphs). No unrelated topics. Ordered from fundamentals to advanced."},
        },
    },
    {
        "key": "plan_chem_vague", "prompt": "plan_create",
        "input": "Student request: I want to study chemistry basics",
        "checks": {
            "plan": {"days": [3, 14], "min_tasks": 2, "language": "en"},
            "judge": {"rubric": "The plan must stay inside basic chemistry (atoms, periodic table, bonding, reactions, moles...). No physics or math-only days. Ordered from fundamentals to advanced."},
        },
    },
    {
        "key": "plan_newton_3days", "holdout": True, "prompt": "plan_create",
        "input": "Student request: 3 day plan for Newton's laws of motion",
        "checks": {
            "plan": {"days": [3, 3], "min_tasks": 2, "practice_each_day": True, "language": "en"},
            "judge": {"rubric": "Every day must be about Newton's laws of motion (first, second, third law, force, mass, acceleration, free-body diagrams). No unrelated topics."},
        },
    },
    {
        "key": "plan_quiz_intent", "prompt": "plan_create",
        "input": "Student request: test me on Ohm's law",
        "checks": {"json_intent": "quiz"},
    },
    {
        "key": "plan_offtopic_history", "holdout": True, "prompt": "plan_create",
        "input": "Student request: study plan for World War 2 history in 5 days",
        "checks": {
            "unsupported_key": True,
            "judge": {"rubric": "The AI must NOT build a history study plan. It must reject the request because it is outside Mathematics, Physics and Chemistry."},
        },
    },
    {
        "key": "plan_offtopic_cooking", "prompt": "plan_create",
        "input": "Student request: plan to learn cooking pasta in 4 days",
        "checks": {
            "unsupported_key": True,
            "judge": {"rubric": "The AI must NOT build a cooking study plan. It must reject the request because it is outside Mathematics, Physics and Chemistry."},
        },
    },

    # ------------------------------------------------------------
    # الاختبار (quiz_make)
    # ------------------------------------------------------------
    {
        "key": "quiz_ohm_en", "prompt": "quiz_make",
        "input": "Student message: quiz me on Ohm's law\nNumber of questions: 4",
        "checks": {
            "quiz": {"count": 4, "language": "en"},
            "judge": {"rubric": "Check EVERY question: the option at index 'answer' (0-based) must be the single correct option and the other three must be wrong. score = fraction of fully correct questions."},
        },
    },
    {
        "key": "quiz_newton_ar", "holdout": True, "prompt": "quiz_make",
        "input": "Student message: اختبرني في قانون نيوتن الثاني\nNumber of questions: 4",
        "checks": {
            "quiz": {"count": 4, "language": "ar"},
            "judge": {"rubric": "Check EVERY question: the option at index 'answer' (0-based) must be the single correct option and the other three must be wrong. score = fraction of fully correct questions."},
        },
    },
    {
        "key": "quiz_ph_en", "prompt": "quiz_make",
        "input": "Student message: acids and bases pH\nNumber of questions: 4",
        "checks": {
            "quiz": {"count": 4, "language": "en"},
            "judge": {"rubric": "Check EVERY question: the option at index 'answer' (0-based) must be the single correct option and the other three must be wrong. Verify any calculation. score = fraction of fully correct questions."},
        },
    },
    {
        "key": "quiz_vague", "holdout": True, "prompt": "quiz_make",
        "input": "Student message: quiz me\nNumber of questions: 4",
        "checks": {
            "quiz": {"count": 4},
            "judge": {"rubric": "All questions must be about Mathematics, Physics or Chemistry, and the option at index 'answer' must be correct for every question. score = fraction of fully correct, on-subject questions."},
        },
    },
]
