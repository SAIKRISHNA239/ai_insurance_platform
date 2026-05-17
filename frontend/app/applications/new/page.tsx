'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { applicationsAPI } from '@/lib/api';

// ── Constants ─────────────────────────────────────────────────────────────────

const POLICY_TYPES = [
  { value: 'individual',          label: 'Individual' },
  { value: 'group',               label: 'Group' },
  { value: 'medicare_supplement', label: 'Medicare Supplement' },
  { value: 'dental',              label: 'Dental' },
  { value: 'vision',              label: 'Vision' },
] as const;

const COVERAGE_PRESETS = [
  { label: '$100K',  value: '100000' },
  { label: '$250K',  value: '250000' },
  { label: '$500K',  value: '500000' },
  { label: '$1M',    value: '1000000' },
];

const HEALTH_QUESTIONS: { key: string; label: string; icon: string }[] = [
  { key: 'smoker',                  label: 'Current tobacco / nicotine user',          icon: 'smoking_rooms' },
  { key: 'pre_existing_conditions', label: 'Has chronic or pre-existing conditions',    icon: 'medical_information' },
  { key: 'recent_surgery',          label: 'Surgery or hospitalization in last 12 mo.', icon: 'local_hospital' },
  { key: 'family_history_heart',    label: 'Family history of heart disease / stroke',  icon: 'cardiology' },
  { key: 'current_medications',     label: 'Currently on prescription medication',       icon: 'medication' },
];

// ── Page ──────────────────────────────────────────────────────────────────────

export default function NewApplicationPage() {
  const router = useRouter();

  // Form fields
  const [appNumberSuffix, setAppNumberSuffix] = useState('');
  const [policyType, setPolicyType]           = useState<string>('individual');
  const [coverageLimit, setCoverageLimit]     = useState('250000');
  const [customCoverage, setCustomCoverage]   = useState(false);
  const [health, setHealth]                   = useState<Record<string, boolean>>({
    smoker: false,
    pre_existing_conditions: false,
    recent_surgery: false,
    family_history_heart: false,
    current_medications: false,
  });

  // Submission state
  const [submitting, setSubmitting] = useState(false);
  const [error,      setError]      = useState<string | null>(null);

  function toggleHealth(key: string) {
    setHealth((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!appNumberSuffix.trim()) {
      setError('Application number is required.');
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      await applicationsAPI.submit({
        application_number:       `APP-${appNumberSuffix.trim().toUpperCase()}`,
        policy_type:              policyType,
        requested_coverage_limit: coverageLimit,
        health_questionnaire:     health,
      });
      router.push('/applications');
    } catch (err: unknown) {
      const e = err as { detail?: string | { msg?: string }[] };
      if (Array.isArray(e.detail)) {
        setError(e.detail.map((d) => d.msg ?? String(d)).join('; '));
      } else {
        setError(String(e.detail ?? 'Submission failed. Please try again.'));
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="h-full overflow-hidden relative p-8">
      {/* Background mesh */}
      <div className="absolute top-[10%] left-[5%] w-[500px] h-[500px] bg-primary/10 rounded-full blur-[120px] mix-blend-screen pointer-events-none animate-float" />
      <div className="absolute bottom-[5%] right-[8%] w-[400px] h-[400px] bg-tertiary/10 rounded-full blur-[100px] mix-blend-screen pointer-events-none animate-float" style={{ animationDelay: '3s' }} />

      <div className="max-w-[760px] mx-auto relative z-10 h-full overflow-y-auto no-scrollbar">

        {/* Header */}
        <div className="mb-8">
          <button
            onClick={() => router.back()}
            className="flex items-center gap-2 text-on-surface-variant hover:text-on-surface transition-colors mb-6 font-label-caps group"
          >
            <span className="material-symbols-outlined text-[18px] group-hover:-translate-x-1 transition-transform">arrow_back</span>
            Back to Applications
          </button>
          <h1 className="font-display text-[40px] text-on-surface leading-tight tracking-tight drop-shadow-md">
            New Application
          </h1>
          <p className="font-body-lg text-on-surface-variant mt-2">
            Submit an insurance application for underwriting review.
          </p>
        </div>

        {/* Error banner */}
        {error && (
          <div className="mb-6 rounded-xl bg-error-container/20 border border-error-container/40 px-5 py-4 flex items-start gap-3 animate-in fade-in duration-200">
            <span className="material-symbols-outlined text-error text-[20px] shrink-0 mt-0.5">error</span>
            <p className="font-body-sm text-error">{error}</p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-6">

          {/* Application Number */}
          <div className="glass-card rounded-2xl p-6 shadow-lg">
            <label className="font-label-caps text-on-surface-variant uppercase tracking-widest text-[11px] mb-3 flex items-center gap-2">
              <span className="material-symbols-outlined text-[16px] text-primary">tag</span>
              Application Number
            </label>
            <div className="flex items-center gap-0">
              <span className="px-4 py-3 bg-white/5 border border-white/10 rounded-l-xl font-data-mono text-on-surface-variant text-sm border-r-0">
                APP-
              </span>
              <input
                id="app-number-suffix"
                type="text"
                required
                value={appNumberSuffix}
                onChange={(e) => setAppNumberSuffix(e.target.value.replace(/[^A-Za-z0-9-]/g, ''))}
                placeholder="2024-001"
                className="flex-1 px-4 py-3 bg-white/5 border border-white/10 rounded-r-xl font-data-mono text-on-surface placeholder:text-on-surface-variant/40 outline-none focus:border-primary/50 focus:bg-primary/5 transition-all"
              />
            </div>
            <p className="font-body-sm text-on-surface-variant/60 mt-2 text-sm">
              Will be saved as <span className="font-data-mono text-on-surface">APP-{appNumberSuffix.toUpperCase() || '…'}</span>
            </p>
          </div>

          {/* Policy Type */}
          <div className="glass-card rounded-2xl p-6 shadow-lg">
            <label className="font-label-caps text-on-surface-variant uppercase tracking-widest text-[11px] mb-4 flex items-center gap-2">
              <span className="material-symbols-outlined text-[16px] text-primary">policy</span>
              Policy Type
            </label>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {POLICY_TYPES.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  id={`policy-type-${value}`}
                  onClick={() => setPolicyType(value)}
                  className={`px-4 py-3 rounded-xl border font-label-caps text-sm transition-all duration-200 text-left ${
                    policyType === value
                      ? 'bg-primary/20 border-primary/50 text-primary shadow-[0_0_12px_rgba(77,142,255,0.15)]'
                      : 'bg-white/5 border-white/10 text-on-surface-variant hover:border-white/20 hover:text-on-surface'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Coverage Limit */}
          <div className="glass-card rounded-2xl p-6 shadow-lg">
            <label className="font-label-caps text-on-surface-variant uppercase tracking-widest text-[11px] mb-4 flex items-center gap-2">
              <span className="material-symbols-outlined text-[16px] text-primary">payments</span>
              Requested Coverage Limit
            </label>
            <div className="grid grid-cols-4 gap-3 mb-4">
              {COVERAGE_PRESETS.map(({ label, value }) => (
                <button
                  key={value}
                  type="button"
                  id={`coverage-preset-${value}`}
                  onClick={() => { setCoverageLimit(value); setCustomCoverage(false); }}
                  className={`py-3 rounded-xl border font-label-caps text-sm transition-all duration-200 ${
                    coverageLimit === value && !customCoverage
                      ? 'bg-secondary/20 border-secondary/50 text-secondary shadow-[0_0_12px_rgba(40,167,69,0.15)]'
                      : 'bg-white/5 border-white/10 text-on-surface-variant hover:border-white/20 hover:text-on-surface'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setCustomCoverage(true)}
                className={`px-3 py-2 rounded-lg border font-label-caps text-xs transition-all ${
                  customCoverage
                    ? 'bg-tertiary/20 border-tertiary/40 text-tertiary'
                    : 'bg-white/5 border-white/10 text-on-surface-variant hover:border-white/20'
                }`}
              >
                Custom
              </button>
              {customCoverage && (
                <div className="flex-1 flex items-center gap-0">
                  <span className="px-3 py-2 bg-white/5 border border-white/10 rounded-l-lg font-data-mono text-on-surface-variant text-sm border-r-0">$</span>
                  <input
                    id="custom-coverage-input"
                    type="number"
                    min="1000"
                    step="1000"
                    value={coverageLimit}
                    onChange={(e) => setCoverageLimit(e.target.value)}
                    className="flex-1 px-3 py-2 bg-white/5 border border-white/10 rounded-r-lg font-data-mono text-on-surface outline-none focus:border-primary/50 transition-all text-sm"
                    placeholder="500000"
                  />
                </div>
              )}
              {!customCoverage && (
                <span className="font-data-mono text-on-surface-variant text-sm">
                  Selected: ${parseInt(coverageLimit).toLocaleString()}
                </span>
              )}
            </div>
          </div>

          {/* Health Questionnaire */}
          <div className="glass-card rounded-2xl p-6 shadow-lg">
            <div className="flex items-center gap-2 mb-1">
              <span className="material-symbols-outlined text-[16px] text-tertiary">health_and_safety</span>
              <span className="font-label-caps text-on-surface-variant uppercase tracking-widest text-[11px]">
                Health Declaration
              </span>
            </div>
            <p className="font-body-sm text-on-surface-variant/60 text-sm mb-5">
              This information is used by the AI underwriting engine to determine risk tier.
            </p>
            <div className="space-y-3">
              {HEALTH_QUESTIONS.map(({ key, label, icon }) => (
                <label
                  key={key}
                  htmlFor={`health-${key}`}
                  className={`flex items-center justify-between gap-4 px-4 py-4 rounded-xl border cursor-pointer transition-all duration-200 ${
                    health[key]
                      ? 'bg-tertiary/10 border-tertiary/30'
                      : 'bg-white/3 border-white/8 hover:border-white/15'
                  }`}
                >
                  <div className="flex items-center gap-3">
                    <span className={`material-symbols-outlined text-[20px] transition-colors ${health[key] ? 'text-tertiary' : 'text-on-surface-variant'}`}>
                      {icon}
                    </span>
                    <span className={`font-body-sm text-sm transition-colors ${health[key] ? 'text-on-surface' : 'text-on-surface-variant'}`}>
                      {label}
                    </span>
                  </div>
                  {/* Toggle */}
                  <div className="relative shrink-0">
                    <input
                      id={`health-${key}`}
                      type="checkbox"
                      checked={health[key]}
                      onChange={() => toggleHealth(key)}
                      className="sr-only"
                    />
                    <div
                      onClick={() => toggleHealth(key)}
                      className={`w-12 h-6 rounded-full transition-all duration-300 cursor-pointer relative ${
                        health[key] ? 'bg-tertiary' : 'bg-white/10'
                      }`}
                    >
                      <div className={`absolute top-1 w-4 h-4 rounded-full bg-white shadow-md transition-all duration-300 ${
                        health[key] ? 'left-7' : 'left-1'
                      }`} />
                    </div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Submit */}
          <div className="flex items-center gap-4 pb-8">
            <button
              type="submit"
              id="submit-application-btn"
              disabled={submitting}
              className={`flex-1 py-4 rounded-xl font-label-caps text-sm tracking-widest uppercase transition-all duration-300 flex items-center justify-center gap-2 shadow-lg ${
                submitting
                  ? 'bg-white/10 text-on-surface-variant cursor-wait'
                  : 'bg-primary text-on-primary hover:bg-primary/80 hover:shadow-[0_8px_24px_rgba(77,142,255,0.35)] active:scale-[0.98]'
              }`}
            >
              {submitting ? (
                <>
                  <span className="material-symbols-outlined text-[18px] animate-spin">sync</span>
                  Submitting…
                </>
              ) : (
                <>
                  <span className="material-symbols-outlined text-[18px]">send</span>
                  Submit Application
                </>
              )}
            </button>
            <button
              type="button"
              onClick={() => router.back()}
              className="px-6 py-4 rounded-xl border border-white/10 text-on-surface-variant hover:border-white/20 hover:text-on-surface font-label-caps text-sm transition-all"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
