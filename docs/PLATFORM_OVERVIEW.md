# VIP Connect Admin UI — Platform Overview

## ¿Qué es esto?

El **VIP Connect Admin UI** es la plataforma interna que el equipo de operaciones usa para gestionar las campañas de llamadas salientes de VIP Medical Group. Permite crear, programar, monitorear y analizar campañas de outbound sin necesidad de entrar directamente a la consola de Amazon Connect.

Todo lo que hace la plataforma gira en torno a **contactar pacientes** — recordatorios de citas, seguimientos de leads, reagendamientos de no-shows — de forma organizada, programada y auditable.

---

## Conceptos principales

### Campaigns (Campañas)

Una campaña es una configuración de llamada saliente: a qué lista de personas llamar, qué flujo de llamada usar, desde qué número, con qué capacidad de agentes. Las campañas viven en Amazon Connect y la plataforma las controla desde aquí.

Desde el Admin UI se puede:
- Crear y editar campañas
- Iniciarlas, pausarlas, detenerlas y reanudarlas
- Ver el estado en tiempo real

### Segments (Segmentos)

Un segmento es un **filtro de leads**. Define qué contactos del universo de leads deben incluirse en una campaña. Por ejemplo: "pacientes de NJ que sean New Lead y no hayan recibido llamada en los últimos 7 días".

Los segmentos se configuran con reglas (tipo de campaña, ubicación, grupo, estado del lead) y se pueden verificar antes de usarlos — la plataforma muestra cuántos contactos cumplen los criterios en este momento.

### Plans (Planes Diarios)

Un plan es una **secuencia de campañas que corren durante el día**. Permite organizar qué campañas corren en qué orden y bajo qué horarios, sin tener que activarlas manualmente una por una.

Ejemplo de plan: *"De 9am a 12pm, corre primero la campaña de New Leads NJ, luego la de NJ Cancellation, y finalmente la de No Shows. De 2pm a 6pm, repite el ciclo."*

Los planes tienen:
- **Buckets** (grupos de campañas que corren en paralelo)
- **Horario de trabajo** (qué horas del día están activos)
- **Trigger** (cuándo arranca: a una hora fija, al terminar otro plan, o manualmente)

### Profiles (Perfiles)

Los perfiles son los registros de cada paciente/contacto en Amazon Connect Customer Profiles. Desde la plataforma se pueden buscar y consultar sin necesidad de entrar a Connect directamente. Útil para verificar datos antes de una campaña.

---

## Cómo funciona el flujo completo

```
Leads en Redis (base de datos en memoria)
       ↓
  Segmento filtra los leads relevantes
       ↓
  Campaña en Connect recibe esos leads
       ↓
  Plan orquesta cuándo y en qué orden corren las campañas
       ↓
  Agentes reciben llamadas conectadas automáticamente
       ↓
  Audit log registra cada acción para compliance
```

**¿De dónde vienen los leads?** De Redis, una base de datos en memoria que se alimenta con los datos de los pacientes/leads del sistema de gestión de VIP Medical Group. El equipo de operaciones no gestiona esa base directamente — la plataforma la lee para construir los segmentos.

**¿Quién hace las llamadas realmente?** Amazon Connect. La plataforma es la capa de administración — configura, programa y dispara las campañas, pero Connect es quien marca los números y conecta con los agentes.

---

## Cómo se refresca la información

| Qué | Con qué frecuencia | Dónde se ve |
|---|---|---|
| Estado de campañas (activa/pausada/detenida) | Cada vez que entras a la pantalla, o manualmente | Página de Campaigns |
| Estado de un plan en ejecución | Cada 10 segundos automáticamente | Plan Monitor |
| Conteo de leads en un segmento | Al hacer "Verify" manualmente | Segment Detail |
| Métricas de llamadas | Al cargar la pantalla de métricas | Dashboard |
| Historial de cambios (audit) | En tiempo real al cargar | Audit page |

El monitor de planes (Plan Monitor) es la pantalla con actualización más frecuente — muestra en tiempo real qué bucket está corriendo, cuántas campañas están activas dentro de él, y el progreso general del plan del día.

---

## Voicemail routing (LocationBasedVoicemail-v2)

Cuando un paciente no contesta, el sistema puede dejar un voicemail. El mensaje que se deja depende de:
- **Tipo de campaña** (General, Pain, Vein)
- **Ubicación de la clínica** (NJ - Clifton, NY - Bronx, CA - Irvine, etc.)
- **Grupo del contacto** (New Lead, Cancellation, No Show)

Esta combinación determina qué grabación de audio se reproduce. La tabla de configuración tiene actualmente **210 combinaciones** mapeadas con sus respectivos audios.

---

## Autenticación y acceso

El acceso a la plataforma requiere una cuenta de usuario configurada en el sistema de autenticación de VIP Medical Group. Los usuarios ingresan con sus credenciales corporativas y la sesión expira automáticamente por inactividad (medida de seguridad).

Cada acción que un usuario realiza — crear una campaña, iniciar un plan, modificar un segmento — queda registrada en el **audit log** con quién hizo qué y cuándo.

---

## Dónde vive la plataforma

- **URL de producción**: `https://dprtjww5c9892.cloudfront.net`
- **Infraestructura**: AWS (us-east-1) — completamente en la nube, sin servidores que administrar
- **Amazon Connect Instance**: la misma instancia que usa el equipo de operaciones actualmente

---

## Resumen visual

```
┌─────────────────────────────────────────────────────────┐
│                    VIP Connect Admin UI                  │
│                                                         │
│   Campaigns ──► Qué llamar y cómo                      │
│   Segments  ──► A quién llamar (filtros de leads)       │
│   Plans     ──► Cuándo y en qué orden                   │
│   Profiles  ──► Consultar datos de un paciente          │
│   Audit     ──► Quién hizo qué y cuándo                 │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
              Amazon Connect (hace las llamadas)
                        │
                        ▼
                 Agentes del call center
```
