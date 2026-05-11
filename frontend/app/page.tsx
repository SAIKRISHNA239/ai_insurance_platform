import { redirect } from 'next/navigation';

// Root redirects to the Overview dashboard
export default function RootPage() {
  redirect('/overview');
}
