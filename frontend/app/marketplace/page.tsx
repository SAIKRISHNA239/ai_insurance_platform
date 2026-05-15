'use client';

// app/marketplace/page.tsx — Insurance Marketplace & Discovery

import { useEffect, useState, useRef } from 'react';
import { marketplaceAPI, type MarketplacePlan } from '@/lib/api';

export default function MarketplacePage() {
  const [policies, setPolicies] = useState<MarketplacePlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [chatInput, setChatInput] = useState('');
  
  // Chat auto-scroll
  const chatScrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Fetch simulated dynamic data
    setLoading(true);
    marketplaceAPI.listPlans()
      .then(setPolicies)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="h-full overflow-hidden relative">
      {/* Mesh background effect is applied globally in layout, but we can add floating shapes here */}
      <div className="absolute top-[10%] left-[20%] w-[400px] h-[400px] bg-primary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[10%] right-[10%] w-[500px] h-[500px] bg-secondary/10 rounded-full blur-[150px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '2s' }} />

      <main className="relative h-full flex flex-col lg:flex-row gap-6 p-8 overflow-hidden z-10">

        {/* ── Left: Policy Grid ────────────────────────────────────────── */}
        <div className="flex-1 flex flex-col gap-6 overflow-y-auto pr-2 no-scrollbar">
          <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
            <div>
              <h2 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">
                Available Policies
              </h2>
              <p className="font-body-lg text-on-surface-variant mt-2 max-w-xl">
                Discover and compare enterprise-grade risk mitigation plans powered by advanced underwriting intelligence.
              </p>
            </div>
            <div className="flex gap-2">
              {['filter_list', 'sort'].map((icon, i) => (
                <button key={icon} className="px-4 py-2 rounded-lg glass-panel flex items-center gap-2 font-body-sm hover:bg-surface-variant/50 transition-all text-on-surface hover:scale-105 active:scale-95">
                  <span className="material-symbols-outlined text-[18px]">{icon}</span>
                  {['Filter', 'Sort'][i]}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 mt-4">
            {loading ? (
              // Loading Skeletons
              Array.from({ length: 4 }).map((_, i) => (
                <div key={`skel-${i}`} className="glass-card rounded-2xl p-6 flex flex-col min-h-[280px]">
                  <div className="flex justify-between items-start mb-6">
                    <div className="space-y-3">
                      <div className="w-40 h-8 bg-surface-variant/40 rounded-lg animate-pulse" />
                      <div className="w-24 h-5 bg-surface-variant/40 rounded-full animate-pulse" />
                    </div>
                    <div className="w-20 h-10 bg-surface-variant/40 rounded-lg animate-pulse" />
                  </div>
                  <div className="space-y-3 mt-auto">
                    <div className="w-full h-4 bg-surface-variant/40 rounded animate-pulse" />
                    <div className="w-5/6 h-4 bg-surface-variant/40 rounded animate-pulse" />
                    <div className="w-4/6 h-4 bg-surface-variant/40 rounded animate-pulse" />
                  </div>
                </div>
              ))
            ) : (
              // Dynamic Policies
              policies.map((policy) => (
                <div
                  key={policy.id}
                  className={`glass-card rounded-2xl p-6 flex flex-col relative overflow-hidden transition-all duration-300 hover:-translate-y-1 hover:shadow-2xl ${
                    policy.featured
                      ? 'border-primary/40 shadow-[0_8px_32px_rgba(77,142,255,0.15)] ring-1 ring-primary/20'
                      : 'border-white/5 hover:border-white/10'
                  }`}
                >
                  {policy.featured && (
                    <div className="absolute top-0 left-0 w-full h-[3px] bg-gradient-to-r from-primary via-secondary to-primary bg-[length:200%_auto] animate-[gradient_3s_linear_infinite]" />
                  )}
                  
                  <div className="flex justify-between items-start mb-6">
                    <div>
                      <h3 className="font-headline-lg text-on-surface tracking-tight">{policy.name}</h3>
                      <span className={`inline-flex items-center px-2.5 py-1 rounded-full border font-label-caps mt-3 backdrop-blur-md ${policy.badgeClass}`}>
                        {policy.badgeIcon && <span className="material-symbols-outlined text-[12px] mr-1.5">{policy.badgeIcon}</span>}
                        {policy.badgeDot && <span className={`w-1.5 h-1.5 rounded-full mr-1.5 shadow-[0_0_8px_currentColor] ${policy.badgeDot}`} />}
                        {policy.badge}
                      </span>
                    </div>
                    <div className="text-right">
                      <div className={`font-display text-[32px] ${policy.priceColor} drop-shadow-sm`}>{policy.price}</div>
                      <div className="font-body-sm text-on-surface-variant opacity-80">/mo per member</div>
                    </div>
                  </div>
                  
                  <div className="pt-5 border-t border-white/5 flex-1 mt-2">
                    <ul className="flex flex-col gap-3 font-body-sm text-on-surface-variant">
                      {policy.features.map((f, idx) => (
                        <li key={idx} className="flex items-start gap-3">
                          <div className={`mt-0.5 rounded-full bg-surface/50 p-0.5 ${policy.checkColor}`}>
                            <span className="material-symbols-outlined text-[14px]">check</span>
                          </div>
                          <span className="leading-relaxed">{f}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                  
                  <div className="mt-8 flex gap-3">
                    <button className={`flex-1 font-body-sm font-semibold py-3 rounded-xl transition-all duration-300 transform hover:scale-[1.02] active:scale-[0.98] ${
                      policy.featured 
                        ? 'bg-primary text-on-primary shadow-[0_4px_20px_rgba(77,142,255,0.3)] hover:shadow-[0_4px_25px_rgba(77,142,255,0.4)] hover:bg-primary-container' 
                        : 'glass-panel text-on-surface hover:bg-white/5'
                    }`}>
                      View Details
                    </button>
                    <button className="px-5 glass-panel text-on-surface font-body-sm font-medium rounded-xl transition-all hover:bg-white/5 hover:text-primary">
                      Compare
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* ── Right: AI Chat Assistant ─────────────────────────────────── */}
        <aside className="w-full lg:w-[400px] shrink-0 glass-panel rounded-2xl overflow-hidden flex flex-col h-full lg:h-auto lg:max-h-full border border-white/10 shadow-[0_8px_32px_rgba(0,0,0,0.4)]">
          {/* Chat header */}
          <div className="px-5 py-4 flex items-center justify-between border-b border-white/5 bg-white/[0.02] shrink-0 backdrop-blur-xl">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-primary/20 border border-primary/30 flex items-center justify-center">
                <span className="material-symbols-outlined text-primary text-[18px]">psychology</span>
              </div>
              <h3 className="font-headline-md text-on-surface text-[16px] tracking-wide">Intelligence Assistant</h3>
            </div>
            <button className="text-on-surface-variant hover:text-primary transition-colors p-1 rounded-full hover:bg-white/5">
              <span className="material-symbols-outlined text-[20px]">more_horiz</span>
            </button>
          </div>

          {/* Messages */}
          <div ref={chatScrollRef} className="flex-1 overflow-y-auto p-5 flex flex-col gap-5 relative bg-gradient-to-b from-transparent to-black/20">
            <div className="text-center sticky top-0 z-10">
              <span className="font-label-caps text-on-surface-variant border border-white/10 rounded-full px-4 py-1.5 glass-panel shadow-lg backdrop-blur-md">
                Session connected to Underwriting DB
              </span>
            </div>

            {/* User */}
            <div className="flex flex-col items-end gap-1 mt-4 animate-in slide-in-from-bottom-2">
              <div className="bg-surface-variant/60 backdrop-blur-md text-on-surface px-5 py-3 rounded-2xl rounded-tr-sm max-w-[85%] font-body-sm leading-relaxed border border-white/5 shadow-md">
                I&apos;m looking for the best policy for a 45-year-old smoker with a history of asthma.
              </div>
              <span className="font-label-caps text-on-surface-variant text-[9px] opacity-70 mt-1">10:42 AM</span>
            </div>

            {/* AI */}
            <div className="flex flex-col items-start gap-1 animate-in slide-in-from-bottom-2" style={{ animationDelay: '150ms' }}>
              <div className="flex items-end gap-3 max-w-[95%]">
                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary/30 to-primary/10 border border-primary/40 flex items-center justify-center shrink-0 mb-1 shadow-[0_0_15px_rgba(77,142,255,0.15)]">
                  <span className="material-symbols-outlined text-primary text-[14px]">auto_awesome</span>
                </div>
                <div className="glass-card border-white/10 text-on-surface px-5 py-4 rounded-2xl rounded-tl-sm font-body-sm leading-relaxed shadow-lg">
                  Based on your criteria, <strong className="text-primary font-semibold drop-shadow-[0_0_8px_rgba(77,142,255,0.5)]">&apos;Health Max&apos;</strong> offers the best asthma-specific coverage at a competitive premium.
                  <br /><br />
                  It includes high-tier pharmacy benefits for inhalers and specialized pulmonologist network access.
                </div>
              </div>
              <span className="font-label-caps text-on-surface-variant text-[9px] opacity-70 ml-10 mt-1">10:43 AM</span>
            </div>
          </div>

          {/* Input area */}
          <div className="glass-panel border-t border-white/5 p-4 flex flex-col gap-4 shrink-0 bg-black/40">
            <div className="flex gap-2 overflow-x-auto no-scrollbar pb-1">
              <button className="whitespace-nowrap font-label-caps text-secondary border border-secondary/30 bg-secondary/10 px-4 py-2 rounded-full hover:bg-secondary/20 hover:scale-105 transition-all shadow-sm">
                Compare Health Max vs Care Elite
              </button>
              <button className="whitespace-nowrap font-label-caps text-on-surface-variant border border-white/10 bg-white/5 px-4 py-2 rounded-full hover:bg-white/10 hover:text-on-surface hover:scale-105 transition-all shadow-sm">
                View formulary list
              </button>
            </div>
            <div className="relative flex items-center group">
              <input
                type="text"
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                placeholder="Ask a follow-up question..."
                className="w-full bg-surface/50 backdrop-blur-md border border-white/10 rounded-xl pl-4 pr-12 py-3 font-body-sm focus:ring-2 focus:ring-primary/50 focus:border-primary/50 focus:bg-surface/80 transition-all placeholder:text-on-surface-variant/50 text-on-surface shadow-inner"
              />
              <button 
                className={`absolute right-2 p-2 rounded-lg transition-all flex items-center justify-center ${
                  chatInput.trim().length > 0 
                    ? 'bg-primary text-on-primary shadow-[0_0_15px_rgba(77,142,255,0.4)] scale-100' 
                    : 'text-on-surface-variant hover:text-primary hover:bg-white/5 scale-95'
                }`}
              >
                <span className="material-symbols-outlined text-[18px]" style={{ fontVariationSettings: "'FILL' 1" }}>send</span>
              </button>
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}
