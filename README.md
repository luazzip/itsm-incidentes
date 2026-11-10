# itsm-incidentes
Microservicio de Incidentes del SaaS ITSM. API Gateway + Lambda (Python 3.12) + DynamoDB,
protegido con el Lambda Authorizer central de itsm-auth. Serverless Framework 3, us-east-1, stage dev, LabRole.

## Estado en esta fase (Contrato de Integración v1.1)
- SNS, SQS, DLQ y consumidores **pospuestos** por revisión del profesor.
- `EVENTS_ENABLED=false`: `publicar_evento()` no publica y nunca rompe el CRUD (deja log `evento_omitido`).
- `src/consumers/` se conserva como código futuro, fuera del despliegue.
- `GET /notificaciones` responde una lista vacía hasta que se active la fase de eventos.

## Requisitos
- itsm-auth desplegado en la misma cuenta (parámetro SSM `/itsm/dev/authorizer-arn`).
- Serverless Framework 3: `sudo npm install -g serverless@3.40.0`

## Despliegue
    aws sts get-caller-identity      # confirmar la cuenta del grupo
    serverless deploy

## Datos ficticios (10,000 por tenant; reproducibles)
    pip3 install boto3 faker
    python3 seed/seed_incidentes.py dev acme             # hito inicial: 1 tenant
    python3 seed/seed_incidentes.py dev globex,initech   # completa los 30,000
IDs: `inc-{tenant}-000001` ... `inc-{tenant}-010000`. Solicitantes `usr-{tenant}-00102..10000`,
agentes `usr-{tenant}-00002..00101` (según el seed de itsm-auth). Ejecutar el seed antes de crear incidentes por la API.

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
| GET | /notificaciones (inactivo en esta fase) | Todos |

## Activar la fase de eventos (solo cuando se autorice)
1. Desplegar itsm-infra.
2. En serverless.yml: `EVENTS_ENABLED: 'true'` y `SNS_EVENTOS_ARN: ${ssm:/itsm/${sls:stage}/sns-eventos-arn}`.
3. Descomentar `consumirQIncidentes` y `consumirQNotificaciones` y quitar `'!src/consumers/**'` de `package.patterns`.
4. `serverless deploy`.
Publica: incidente.creado, incidente.escalado_a_problema, incidente.resuelto.
Consume: problema.resuelto (q-incidentes) y todos los eventos (q-notificaciones).

## Modelo de datos (DynamoDB)
**itsm-incidentes-dev-incidentes**: PK tenant_id, SK incidente_id
```json
{"tenant_id":"acme","incidente_id":"inc-acme-000001","titulo":"VPN no conecta","descripcion":"...",
 "categoria":"RED|HARDWARE|SOFTWARE|ACCESOS|CORREO|OTROS","impacto":"ALTO|MEDIO|BAJO","urgencia":"ALTA|MEDIA|BAJA",
 "prioridad":"P1|P2|P3|P4","estado":"NUEVO|ASIGNADO|EN_PROGRESO|RESUELTO|CERRADO",
 "solicitante_id":"usr-acme-00142","asignado_a":"usr-acme-00003","problema_id":null,
 "escalado":false,"adjuntos":["acme/incidentes/inc-acme-000001/captura.png"],
 "sla_vence_en":"2026-11-06T10:00:00Z","creado_en":"2026-11-05T14:30:00Z","actualizado_en":"2026-11-05T14:30:00Z",
 "estado_creado":"NUEVO#2026-11-05T14:30:00Z","asignado_creado":"usr-acme-00003#2026-11-05T14:30:00Z",
 "solicitante_creado":"usr-acme-00142#2026-11-05T14:30:00Z"}
```
GSI1: tenant_id + estado_creado · GSI2: tenant_id + asignado_creado · GSI3: tenant_id + solicitante_creado

**itsm-incidentes-dev-comentarios**: PK tenant_incidente (tenant_id#incidente_id), SK comentario_id
```json
{"tenant_incidente":"acme#inc-acme-000001","comentario_id":"com-20261110203000123456-a1b2c3",
 "autor_id":"usr-acme-00003","autor_rol":"AGENTE","texto":"...","creado_en":"..."}
```
**itsm-incidentes-dev-notificaciones** (auxiliar, se llena en la fase de eventos): PK tenant_id, SK notificacion_id
**itsm-incidentes-dev-contadores** (auxiliar): PK tenant_id, SK entidad → `{"tenant_id":"acme","entidad":"incidente","valor":10001}`
