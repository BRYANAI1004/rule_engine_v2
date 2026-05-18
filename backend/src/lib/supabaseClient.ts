import { createClient, type SupabaseClient } from '@supabase/supabase-js';

import { env } from './env.js';

export function createSupabaseAdminClient(): SupabaseClient {
  const url = env.SUPABASE_URL?.trim();
  const serviceRoleKey = env.SUPABASE_SERVICE_ROLE_KEY?.trim();

  if (!url || !serviceRoleKey) {
    throw new Error(
      'Missing Supabase configuration: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (service role key) before calling createSupabaseAdminClient().',
    );
  }

  return createClient(url, serviceRoleKey, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
    },
  });
}
