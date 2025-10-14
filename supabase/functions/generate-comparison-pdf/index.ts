// Importa los módulos necesarios de Deno y Supabase.
import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.44.4'

// --- Interfaces de TypeScript que coinciden con los modelos Pydantic de Python ---

// Corresponde a la clase PlanInput en server.py
interface PlanInput {
  nombre: string;
  precio_potencia: Record<string, number>;
  precio_energia: Record<string, number>;
  cargos_fijos_anual_eur: number;
}

// Corresponde a la clase SuministroInfo en server.py
interface SuministroInfo {
  direccion: string;
  cif: string;
  fecha_estudio: string;
  poblacion: string;
  cups: string;
  nombre_cliente?: string;
}

// Corresponde a la clase MonthlyInput en server.py, que es el payload principal.
interface ComparisonPayload {
  tarifa: '2.0TD' | '3.0TD' | '6.1TD';
  energia_kwh_mes: Record<string, number[]>;
  potencia_contratada_kw: Record<string, number>;
  actual: PlanInput;
  propuesta: PlanInput;
  iva_pct: number;
  impuesto_electricidad_pct: number;
  suministro: SuministroInfo;
  
  // Campos adicionales que pasaremos desde el frontend para la tabla 'comparativas'.
  cliente_id?: string;
  punto_id?: string;
}

// --- Lógica Principal de la Edge Function ---

serve(async (req) => {
  // Manejo de CORS: Permite las peticiones pre-flight (OPTIONS) desde cualquier origen.
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: { 
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-openenergies-app',
    } });
  }

  try {
    // 1. Validar que el método de la petición es POST.
    if (req.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    // 2. Crear el cliente de Supabase con privilegios de administrador para operaciones internas.
    const supabaseAdmin = createClient(
      Deno.env.get('SUPABASE_URL')!,
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    );

    // 3. Autenticar al usuario que realiza la petición.
    // Se crea un cliente temporal usando el token 'Authorization' de la petición entrante.
    const supabaseUserClient = createClient(
      Deno.env.get('SUPABASE_URL')!,
      Deno.env.get('SUPABASE_ANON_KEY')!,
      { global: { headers: { Authorization: req.headers.get('Authorization')! } } }
    );
    const { data: { user } } = await supabaseUserClient.auth.getUser();

    if (!user) {
      return new Response(JSON.stringify({ error: 'Authentication required.' }), { status: 401, headers: { 'Content-Type': 'application/json' } });
    }

    // 4. Autorizar al usuario.
    // Verificamos en la tabla 'usuarios_app' que el rol del usuario sea 'administrador'.
    const { data: userProfile, error: profileError } = await supabaseAdmin
      .from('usuarios_app')
      .select('rol')
      .eq('user_id', user.id)
      .single();

    if (profileError || !userProfile || userProfile.rol !== 'administrador') {
      return new Response(JSON.stringify({ error: 'Forbidden: Only administrators can perform this action.' }), { status: 403, headers: { 'Content-Type': 'application/json' } });
    }

    // 5. Obtener los datos del cuerpo de la petición.
    const payload: ComparisonPayload = await req.json();

    // 6. Llamar de forma segura al microservicio en Google Cloud Run.
    const cloudRunUrl = Deno.env.get('CLOUD_RUN_URL');
    const apiToken = Deno.env.get('INTERNAL_API_TOKEN');

    if (!cloudRunUrl || !apiToken) {
      throw new Error('Cloud Run URL or API Token are not configured in Supabase secrets.');
    }

    const pdfResponse = await fetch(cloudRunUrl + '/generate-report', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Internal-Auth-Token': apiToken, // El token secreto para la comunicación server-to-server.
      },
      body: JSON.stringify(payload),
    });

    if (!pdfResponse.ok) {
      const errorBody = await pdfResponse.text();
      throw new Error(`PDF generation service failed with status ${pdfResponse.status}: ${errorBody}`);
    }

    const pdfBlob = await pdfResponse.blob();
    
    // 7. Subir el PDF generado a Supabase Storage.
    const filePath = `comparativas/${user.id}/${crypto.randomUUID()}.pdf`;
    const { error: uploadError } = await supabaseAdmin.storage
      .from('documentos') // Confirma que 'documentos' es el nombre correcto de tu bucket.
      .upload(filePath, pdfBlob, { contentType: 'application/pdf', upsert: false });

    if (uploadError) {
      throw new Error(`Failed to upload PDF to storage: ${uploadError.message}`);
    }

    // 8. Guardar la referencia del PDF en la tabla 'comparativas'.
    const { data: newComparison, error: insertError } = await supabaseAdmin
      .from('comparativas')
      .insert({
        creado_por_user_id: user.id,
        cliente_id: payload.cliente_id || null,
        punto_id: payload.punto_id || null,
        prospecto_nombre: payload.suministro?.nombre_cliente || null,
        ruta_pdf: filePath,
      })
      .select('id')
      .single();

    if (insertError) {
      // Intento de limpieza: si falla la inserción en BBDD, borramos el PDF huérfano.
      await supabaseAdmin.storage.from('documentos').remove([filePath]);
      throw new Error(`Failed to save comparison to database: ${insertError.message}`);
    }

    // 9. Devolver una respuesta de éxito al frontend con los datos relevantes.
    return new Response(JSON.stringify({ 
      success: true, 
      comparisonId: newComparison.id,
      filePath: filePath 
    }), {
      headers: { 
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      },
      status: 200,
    });

  } catch (error) {
    // --- BLOQUE DE ERROR MEJORADO ---
    // Imprime el error completo en los logs de Supabase para depuración.
    console.error('Error in Edge Function:', error);

    // Devuelve un mensaje de error detallado al frontend.
    return new Response(JSON.stringify({ 
      error: "An internal error occurred in the Edge Function.",
      details: error.message,
      stack: error.stack // El stack trace nos dirá exactamente en qué línea falló
    }), {
      headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      status: 500,
    });
  }
});