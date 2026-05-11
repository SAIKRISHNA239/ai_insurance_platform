'use client';

export default function SupportPage() {
  const faqs = [
    {
      q: 'How do I approve an underwriting application?',
      a: 'Open the Underwriting Desk, select an application from the Review Queue, click "Analyze" to generate the AI medical summary, then use the Approve, Decline, or Postpone buttons.',
    },
    {
      q: 'What do the fraud score percentages mean?',
      a: 'The fraud score is an AI-generated probability (0–100%) that a claim contains anomalous billing patterns. Scores above 50% trigger automatic manual review.',
    },
    {
      q: 'What does the AI Risk Tier mean?',
      a: 'Risk tiers (Preferred → Standard → Substandard → Decline) are assigned by the underwriting AI based on the applicant\'s medical history, diagnosis codes, and actuarial tables.',
    },
    {
      q: 'How are claims ingested?',
      a: 'The Claims Center supports drag-and-drop ingestion of PDF, PNG, and JPG documents. The AI extracts ICD-10 codes, billed amounts, and diagnosis narratives automatically.',
    },
    {
      q: 'Where are credentials stored?',
      a: 'JWT access tokens are stored in localStorage for development. In production, they should be stored in httpOnly cookies managed by the Next.js backend.',
    },
  ];

  return (
    <div className="h-full overflow-y-auto p-8 bg-background">
      <div className="max-w-2xl mx-auto space-y-6">
        <div>
          <h2 className="font-headline-lg text-headline-lg text-on-surface">Support & Documentation</h2>
          <p className="font-body-sm text-body-sm text-on-surface-variant mt-1">
            Frequently asked questions and platform guidance.
          </p>
        </div>

        {/* Hero banner */}
        <div className="bg-gradient-to-br from-primary/10 to-secondary/10 border border-primary/20 rounded-xl p-6 flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-primary/20 flex items-center justify-center shrink-0">
            <span className="material-symbols-outlined text-primary text-2xl">support_agent</span>
          </div>
          <div>
            <p className="font-headline-md text-headline-md text-on-surface">Need help?</p>
            <p className="font-body-sm text-body-sm text-on-surface-variant mt-1">
              Contact the platform team at{' '}
              <a href="mailto:admin@medintel.ai" className="text-primary underline">
                admin@medintel.ai
              </a>
            </p>
          </div>
        </div>

        {/* FAQs */}
        <div className="bg-surface-container-low border border-outline-variant rounded-xl overflow-hidden">
          <div className="px-5 py-3.5 border-b border-outline-variant bg-surface-container flex items-center gap-2">
            <span className="material-symbols-outlined text-primary text-[20px]">quiz</span>
            <h3 className="font-headline-md text-headline-md text-on-surface">FAQ</h3>
          </div>
          <ul className="divide-y divide-outline-variant/50">
            {faqs.map((faq) => (
              <li key={faq.q} className="px-5 py-4">
                <p className="font-body-sm text-body-sm text-on-surface font-semibold flex items-start gap-2">
                  <span className="material-symbols-outlined text-primary text-[18px] mt-0.5 shrink-0">help</span>
                  {faq.q}
                </p>
                <p className="font-body-sm text-body-sm text-on-surface-variant mt-2 ml-7 leading-relaxed">
                  {faq.a}
                </p>
              </li>
            ))}
          </ul>
        </div>

        {/* Quick links */}
        <div className="grid grid-cols-2 gap-3">
          {[
            { label: 'Backend API Docs', icon: 'api', href: 'http://localhost:8000/docs', sub: 'FastAPI Swagger UI' },
            { label: 'Platform GitHub', icon: 'code', href: '#', sub: 'Source repository' },
          ].map((link) => (
            <a
              key={link.label}
              href={link.href}
              target="_blank"
              rel="noreferrer"
              className="bg-surface-container-low border border-outline-variant rounded-xl p-4 flex items-center gap-3 hover:border-primary/40 hover:bg-primary/5 transition-colors"
            >
              <span className="material-symbols-outlined text-primary">{link.icon}</span>
              <div>
                <p className="font-body-sm text-body-sm text-on-surface font-semibold">{link.label}</p>
                <p className="font-label-caps text-label-caps text-on-surface-variant">{link.sub}</p>
              </div>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
