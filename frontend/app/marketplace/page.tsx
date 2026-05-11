// app/marketplace/page.tsx — Insurance Marketplace & Discovery

const POLICIES = [
  {
    name: 'Care Elite',
    badge: 'Comprehensive',
    badgeClass: 'bg-secondary-container/20 text-secondary border-secondary/30',
    badgeDot: 'bg-secondary',
    price: '$850',
    priceColor: 'text-primary',
    features: ['Full catastrophic risk coverage', 'No deductible for chronic care', 'Premium network access'],
    checkColor: 'text-secondary',
    featured: false,
  },
  {
    name: 'Health Max',
    badge: 'AI Recommended',
    badgeClass: 'bg-primary-container/20 text-primary border-primary/30',
    badgeDot: null,
    badgeIcon: 'auto_awesome',
    price: '$620',
    priceColor: 'text-primary',
    features: ['Optimized for chronic conditions (Asthma)', 'High-tier pharmacy benefits', 'Telehealth specialist network'],
    checkColor: 'text-primary',
    featured: true,
  },
  {
    name: 'Base Shield',
    badge: 'Essential',
    badgeClass: 'bg-surface-variant text-on-surface-variant border-outline-variant',
    badgeDot: null,
    price: '$340',
    priceColor: 'text-on-surface-variant',
    features: ['Standard preventive care', 'High deductible model', 'Local network only'],
    checkColor: 'text-outline',
    featured: false,
  },
];

export default function MarketplacePage() {
  return (
    <div className="h-full overflow-hidden">
      <main className="h-full flex flex-col lg:flex-row gap-4 p-8 overflow-hidden bg-surface-container-lowest">

        {/* ── Left: Policy Grid ────────────────────────────────────────── */}
        <div className="flex-1 flex flex-col gap-6 overflow-y-auto pr-2">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="font-headline-lg text-headline-lg text-on-surface">Available Policies</h2>
              <p className="font-body-sm text-body-sm text-on-surface-variant mt-1">
                Discover and compare enterprise-grade risk mitigation plans.
              </p>
            </div>
            <div className="flex gap-2">
              {['filter_list', 'sort'].map((icon, i) => (
                <button key={icon} className="px-3 py-1.5 rounded border border-outline-variant flex items-center gap-2 font-body-sm text-body-sm hover:bg-surface-variant transition-colors text-on-surface">
                  <span className="material-symbols-outlined text-[18px]">{icon}</span>
                  {['Filter', 'Sort'][i]}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {POLICIES.map((policy) => (
              <div
                key={policy.name}
                className={`bg-surface-container-low rounded-xl p-5 flex flex-col relative overflow-hidden ${
                  policy.featured
                    ? 'border border-primary/50 shadow-[0_0_15px_rgba(77,142,255,0.05)]'
                    : 'border border-outline-variant'
                }`}
              >
                {policy.featured && (
                  <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-primary to-secondary" />
                )}
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="font-headline-md text-headline-md text-on-surface">{policy.name}</h3>
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full border font-label-caps text-label-caps mt-2 ${policy.badgeClass}`}>
                      {policy.badgeIcon && <span className="material-symbols-outlined text-[12px] mr-1">{policy.badgeIcon}</span>}
                      {policy.badgeDot && <span className={`w-1.5 h-1.5 rounded-full mr-1.5 ${policy.badgeDot}`} />}
                      {policy.badge}
                    </span>
                  </div>
                  <div className="text-right">
                    <div className={`font-display text-display ${policy.priceColor}`}>{policy.price}</div>
                    <div className="font-body-sm text-body-sm text-on-surface-variant">/mo per member</div>
                  </div>
                </div>
                <div className="border-t border-outline-variant pt-4 flex-1">
                  <ul className="flex flex-col gap-2 font-body-sm text-body-sm text-on-surface-variant">
                    {policy.features.map((f) => (
                      <li key={f} className="flex items-start gap-2">
                        <span className={`material-symbols-outlined text-[18px] ${policy.checkColor}`}>check_circle</span>
                        {f}
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="mt-6 flex gap-3">
                  <button className={`flex-1 font-body-sm text-body-sm font-medium py-2 rounded hover:opacity-90 transition-colors ${policy.featured ? 'bg-primary text-on-primary' : 'border border-outline-variant text-on-surface hover:bg-surface-variant'}`}>
                    View Details
                  </button>
                  <button className="px-4 border border-outline-variant text-on-surface font-body-sm text-body-sm font-medium rounded hover:bg-surface-variant transition-colors">
                    Compare
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Right: AI Chat Assistant ─────────────────────────────────── */}
        <aside className="w-full lg:w-96 shrink-0 bg-surface-container-low border border-outline-variant rounded-xl overflow-hidden flex flex-col h-full lg:h-auto lg:max-h-full">
          {/* Chat header */}
          <div className="bg-surface-container-high px-4 py-3 flex items-center justify-between border-b border-outline-variant shrink-0">
            <div className="flex items-center gap-2">
              <span className="material-symbols-outlined text-primary">psychology</span>
              <h3 className="font-headline-md text-headline-md text-on-surface text-[16px]">Intelligence Assistant</h3>
            </div>
            <button className="text-on-surface-variant hover:text-on-surface transition-colors">
              <span className="material-symbols-outlined text-[20px]">more_horiz</span>
            </button>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4 bg-surface-container-lowest">
            <div className="text-center">
              <span className="font-label-caps text-label-caps text-on-surface-variant border border-outline-variant rounded-full px-3 py-1 bg-surface-container">
                Session connected to Underwriting DB
              </span>
            </div>

            {/* User */}
            <div className="flex flex-col items-end gap-1 mt-2">
              <div className="bg-surface-variant text-on-surface px-4 py-2.5 rounded-2xl rounded-tr-sm max-w-[85%] font-body-sm text-body-sm">
                I&apos;m looking for the best policy for a 45-year-old smoker with a history of asthma.
              </div>
              <span className="font-label-caps text-label-caps text-on-surface-variant text-[9px]">10:42 AM</span>
            </div>

            {/* AI */}
            <div className="flex flex-col items-start gap-1">
              <div className="flex items-end gap-2 max-w-[90%]">
                <div className="w-6 h-6 rounded-full bg-primary/20 border border-primary/30 flex items-center justify-center shrink-0 mb-1">
                  <span className="material-symbols-outlined text-primary text-[14px]">auto_awesome</span>
                </div>
                <div className="bg-surface-container-high border border-outline-variant text-on-surface px-4 py-3 rounded-2xl rounded-tl-sm font-body-sm text-body-sm">
                  Based on your criteria, <strong className="text-primary font-semibold">&apos;Health Max&apos;</strong> offers the best asthma-specific coverage at a competitive premium.
                  <br /><br />
                  It includes high-tier pharmacy benefits for inhalers and specialized pulmonologist network access.
                </div>
              </div>
              <span className="font-label-caps text-label-caps text-on-surface-variant text-[9px] ml-8">10:43 AM</span>
            </div>
          </div>

          {/* Input area */}
          <div className="bg-surface-container-high border-t border-outline-variant p-3 flex flex-col gap-3 shrink-0">
            <div className="flex gap-2 overflow-x-auto no-scrollbar">
              <button className="whitespace-nowrap font-label-caps text-label-caps text-secondary border border-secondary/30 bg-secondary/10 px-3 py-1.5 rounded-full hover:bg-secondary/20 transition-colors">
                Compare Health Max vs Care Elite
              </button>
              <button className="whitespace-nowrap font-label-caps text-label-caps text-on-surface-variant border border-outline-variant bg-surface px-3 py-1.5 rounded-full hover:bg-surface-variant transition-colors">
                View formulary list
              </button>
            </div>
            <div className="relative flex items-center">
              <input
                type="text"
                placeholder="Ask a follow-up question..."
                className="w-full bg-surface border border-outline-variant rounded-lg pl-3 pr-10 py-2 font-body-sm text-body-sm focus:ring-1 focus:ring-primary focus:border-primary focus:outline-none transition-all placeholder:text-on-surface-variant text-on-surface"
              />
              <button className="absolute right-2 text-primary hover:opacity-80 p-1">
                <span className="material-symbols-outlined text-[20px]" style={{ fontVariationSettings: "'FILL' 1" }}>send</span>
              </button>
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}
