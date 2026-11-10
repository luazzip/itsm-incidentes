# itsm-incidentes
Microservicio de Incidentes del SaaS ITSM. API Gateway + Lambda (Python 3.12) + DynamoDB,
protegido con el Lambda Authorizer de itsm-auth. Publica y consume eventos del bus de itsm-infra.

## Despliegue (requiere itsm-infra e itsm-auth desplegados)
    serverless deploy
## Datos ficticios (30,000 = 10,000 x acme, globex, initech)
    pip3 install boto3 faker
    python3 seed/seed_incidentes.py

## Endpoints (header Authorization: Bearer <JWT>)
| Método | Ruta | Roles |
|---|---|---|
| POST | /incidentes | Todos |
| GET | /incidentes?limite&siguiente&estado&prioridad&asignado_a | Todos (SOLICITANTE solo los suyos) |
| GET | /incidentes/{id} | Todos |
| PUT | /incidentes/{id} | ADMIN, AGENTE |
| PATCH | /incidentes/{id}/estado | ADMIN, AGENTE |
| PATCH | /incidentes/{id}/asignar | ADMIN, AGENTE |
| POST | /incidentes/{id}/escalar-problema | ADMIN, AGENTE |
| POST | /incidentes/{id}/comentarios | Todos |
| GET | /incidentes/{id}/comentarios | Todos |
| DELETE | /incidentes/{id} (solo NUEVO) | ADMIN |
| GET | /notificaciones | Todos |

## Eventos
Publica: incidente.creado, incidente.escalado_a_problema, incidente.resuelto
Consume: q-incidentes (problema.resuelto -> incidentes RESUELTOS), q-notificaciones (todos -> tabla notificaciones)

## Modelo de datos (DynamoDB)
**itsm-incidentes-dev-incidentes** — PK tenant_id, SK incidente_id
```json
{"tenant_id":"acme","incidente_id":"inc-acme-000001","titulo":"VPN no conecta","descripcion":"...",
 "categoria":"RED|HARDWARE|SOFTWARE|ACCESOS|CORREO|OTROS","impacto":"ALTO|MEDIO|BAJO","urgencia":"ALTA|MEDIA|BAJA",
 "prioridad":"P1|P2|P3|P4","estado":"NUEVO|ASIGNADO|EN_PROGRESO|RESUELTO|CERRADO",
 "solicitante_id":"usr-acme-00042","asignado_a":"usr-acme-00003","problema_id":"prb-acme-000123",
 "escalado":false,"adjuntos":["acme/incidentes/inc-acme-000001/captura.png"],
 "sla_vence_en":"2026-11-06T10:00:00Z","resuelto_en":"...","cerrado_en":"...",
 "creado_en":"2026-11-05T14:30:00Z","actualizado_en":"2026-11-05T14:30:00Z",
 "estado_creado":"NUEVO#2026-11-05T14:30:00Z","asignado_creado":"usr-acme-00003#2026-11-05T14:30:00Z",
 "solicitante_creado":"usr-acme-00042#2026-11-05T14:30:00Z"}
```
GSI1: tenant_id + estado_creado · GSI2: tenant_id + asignado_creado · GSI3: tenant_id + solicitante_creado

**comentarios** — PK tenant_incidente (tenant_id#incidente_id), SK comentario_id
```json
{"tenant_incidente":"acme#inc-acme-000001","comentario_id":"com-20261108203000123456-a1b2c3",
 "autor_id":"usr-acme-00003","autor_rol":"AGENTE","texto":"...","creado_en":"..."}
```
**notificaciones** — PK tenant_id, SK notificacion_id (creado_en#evento_id)
```json
{"tenant_id":"acme","notificacion_id":"2026-11-08T20:30:00Z#<id>","evento":"incidente.creado",
 "origen":"incidentes","mensaje":"Nuevo incidente inc-acme-010001: VPN no conecta","data":{},"leida":false}
```
**contadores** — PK tenant_id, SK entidad: `{"tenant_id":"acme","entidad":"incidente","valor":10001}`
