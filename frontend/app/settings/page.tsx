'use client';

import { useAuth } from '@/lib/AuthContext';

export default function SettingsPage() {
  const { role } = useAuth();

  const sections = [
    {
      title: 'Account',
      icon: 'manage_accounts',
      rows: [
        { label: 'Role', value: role ? role.charAt(0).toUpperCase() + role.slice(1) : '—', icon: 'badge' },
        { label: 'Session', value: 'Active', icon: 'verified_user' },
        { label: 'MFA', value: 'Enabled', icon: 'security' },
      ],
    },
    {
      title: 'Notifications',
      icon: 'notifications',
      rows: [
        { label: 'Email Alerts', value: 'On', icon: 'email' },
        { label: 'Claim Updates', value: 'Instant', icon: 'assignment_late' },
        { label: 'Underwriting Reminders', value: 'Daily Digest', icon: 'summarize' },
      ],
    },
    {
      title: 'System',
      icon: 'tune',
      rows: [
        { label: 'Backend', value: 'FastAPI · localhost:8000', icon: 'dns' },
        { label: 'AI Engine', value: 'Gemini 1.5 Flash', icon: 'smart_toy' },
        { label: 'Vector Store', value: 'ChromaDB · localhost:8001', icon: 'storage' },
      ],
    },
  ];

  return (
    <div className="h-full overflow-y-auto p-8 bg-background">
      <div className="max-w-2xl mx-auto space-y-6">
        <div>
          <h2 className="font-headline-lg text-headline-lg text-on-surface">Settings</h2>
          <p className="font-body-sm text-body-sm text-on-surface-variant mt-1">
            Platform configuration and account preferences.
          </p>
        </div>

        {sections.map((section) => (
          <div
            key={section.title}
            className="bg-surface-container-low border border-outline-variant rounded-xl overflow-hidden"
          >
            <div className="flex items-center gap-2 px-5 py-3.5 border-b border-outline-variant bg-surface-container">
              <span className="material-symbols-outlined text-primary text-[20px]">{section.icon}</span>
              <h3 className="font-headline-md text-headline-md text-on-surface">{section.title}</h3>
            </div>
            <ul className="divide-y divide-outline-variant/50">
              {section.rows.map((row) => (
                <li key={row.label} className="flex items-center justify-between px-5 py-3.5">
                  <div className="flex items-center gap-3">
                    <span className="material-symbols-outlined text-on-surface-variant text-[18px]">{row.icon}</span>
                    <span className="font-body-sm text-body-sm text-on-surface">{row.label}</span>
                  </div>
                  <span className="font-data-mono text-data-mono text-on-surface-variant">{row.value}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}

        <div className="bg-surface-container-low border border-outline-variant rounded-xl p-5 flex items-center gap-4">
          <span className="material-symbols-outlined text-outline text-3xl">info</span>
          <div>
            <p className="font-body-sm text-body-sm text-on-surface font-semibold">MedIntelligence Platform</p>
            <p className="font-label-caps text-label-caps text-on-surface-variant">v0.1.0 · Enterprise Edition · © 2026</p>
          </div>
        </div>
      </div>
    </div>
  );
}
