# itsm-incidentes
Microservicio de Incidentes del SaaS ITSM. API Gateway + Lambda (Python 3.12) + DynamoDB,
protegido con el Lambda Authorizer central de itsm-auth. Serverless Framework 3, us-east-1, stage dev, LabRole.

## Estado del despliegue

Microservicio desplegado en AWS (`us-east-1`, stage `dev`) con API Gateway, Lambda Python 3.12 y DynamoDB.

- **Publicacion de eventos:** habilitada mediante `EVENTS_ENABLED=true` y el topico SNS compartido `itsm-eventos-dev`.
- **Consumidor de incidentes:** `consumirQIncidentes`, conectado a `itsm-q-incidentes-dev`.
- **Consumidor de notificaciones:** `consumirQNotificaciones`, conectado a `itsm-q-notificaciones-dev`.
- **Procesamiento SQS:** habilitado, con soporte para `ReportBatchItemFailures`.
- **Persistencia de notificaciones:** tabla `itsm-incidentes-dev-notificaciones`.
- **Aislamiento multi-tenant:** aplicado mediante el contexto del Lambda Authorizer.

Se verifico la creacion automatica de un problema al escalar un incidente y la resolucion automatica del incidente al recibir `problema.resuelto`.

Tambien se comprobo la persistencia de notificaciones procedentes del bus de eventos.

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
| GET | /notificaciones | Todos |

## Integracion por eventos

Los productores publican mensajes en SNS con los atributos `evento` y `tenant_id`. Las suscripciones SNS distribuyen los mensajes a las colas SQS correspondientes.

**Eventos publicados:**
- `incidente.creado`
- `incidente.escalado_a_problema`
- `incidente.resuelto`

**Eventos consumidos:**
- `problema.resuelto`: permite resolver incidentes vinculados al problema.
- Eventos recibidos mediante `q-notificaciones`: se almacenan en DynamoDB para su consulta.

Infra administra las colas, sus DLQ, las suscripciones SNS y las alarmas CloudWatch.

**Prueba de integracion verificada:**

- Incidente: `inc-acme-010001`
- Problema: `prb-acme-evt856d7948b6b3`
- Cambio: `chg-acme-e40688e8e5ca`

Despues de implementar el cambio, Problemas publico `problema.resuelto` y el incidente original paso automaticamente a `RESUELTO`.
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
**itsm-incidentes-dev-notificaciones** (persistencia de notificaciones recibidas por SQS): PK tenant_id, SK notificacion_id
**itsm-incidentes-dev-contadores** (auxiliar): PK tenant_id, SK entidad → `{"tenant_id":"acme","entidad":"incidente","valor":10001}`
