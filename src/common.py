"""Utilidades compartidas: respuestas, token (tenant), paginacion, DynamoDB, eventos SNS y logs."""
import base64
import functools
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

# Clientes fuera del handler: se reutilizan mientras la Lambda esta "caliente"
dynamodb = boto3.resource('dynamodb')

TABLA_INCIDENTES = dynamodb.Table(os.environ['TABLA_INCIDENTES'])
TABLA_COMENTARIOS = dynamodb.Table(os.environ['TABLA_COMENTARIOS'])
TABLA_NOTIFICACIONES = dynamodb.Table(os.environ['TABLA_NOTIFICACIONES'])
TABLA_CONTADORES = dynamodb.Table(os.environ['TABLA_CONTADORES'])

# Eventos POSPUESTOS en esta fase (Contrato v1.1): solo se publica si EVENTS_ENABLED=true y hay ARN
EVENTOS_ACTIVOS = os.environ.get('EVENTS_ENABLED', 'false').strip().lower() == 'true'
SNS_EVENTOS_ARN = os.environ.get('SNS_EVENTOS_ARN')  # opcional
sns = boto3.client('sns') if EVENTOS_ACTIVOS and SNS_EVENTOS_ARN else None

ROLES = ('ADMIN', 'AGENTE', 'SOLICITANTE')


class ErrorApi(Exception):
    """Error controlado: se convierte en {"error": {"codigo", "mensaje"}} con su codigo HTTP."""

    def __init__(self, status, codigo, mensaje):
        super().__init__(mensaje)
        self.status = status
        self.codigo = codigo
        self.mensaje = mensaje


# ---------------------------------------------------------------- fechas y logs
def ahora_dt():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(momento):
    return momento.strftime('%Y-%m-%dT%H:%M:%SZ')


def _a_json(valor):
    if isinstance(valor, Decimal):
        return int(valor) if valor % 1 == 0 else float(valor)
    if isinstance(valor, set):
        return list(valor)
    return str(valor)


def log(tipo, **datos):
    # Formato estandar del Taller de Monitoreo: {"tipo": "INFO"/"ERROR", "log_datos": {...}}
    print(json.dumps({'tipo': tipo, 'log_datos': datos}, default=_a_json, ensure_ascii=False))


# ---------------------------------------------------------------- respuestas HTTP
def respuesta(status, cuerpo):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps(cuerpo, default=_a_json, ensure_ascii=False),
    }


def ok(data, status=200, paginacion=None):
    cuerpo = {'data': data}
    if paginacion is not None:
        cuerpo['paginacion'] = paginacion
    return respuesta(status, cuerpo)


def api(funcion):
    """Envuelve cada handler: errores controlados -> 4xx, cualquier otro -> 500 + log ERROR."""

    @functools.wraps(funcion)
    def envoltura(event, context):
        try:
            return funcion(event, context)
        except ErrorApi as e:
            log('INFO', funcion=funcion.__name__, status=e.status, codigo=e.codigo, mensaje=e.mensaje)
            return respuesta(e.status, {'error': {'codigo': e.codigo, 'mensaje': e.mensaje}})
        except Exception as e:  # noqa: BLE001
            log('ERROR', funcion=funcion.__name__, error=repr(e))
            return respuesta(500, {'error': {'codigo': 'ERROR_INTERNO', 'mensaje': 'Error interno del servidor'}})

    return envoltura


# ---------------------------------------------------------------- lectura del request
def leer_body(event):
    body = event.get('body', event)  # misma regla de clase: body = event.get('body', event)
    if body is None:
        return {}
    if isinstance(body, str):
        if not body.strip():
            return {}
        try:
            body = json.loads(body, parse_float=Decimal)
        except ValueError:
            raise ErrorApi(400, 'VALIDACION', 'El body no es un JSON valido')
    if not isinstance(body, dict):
        raise ErrorApi(400, 'VALIDACION', 'El body debe ser un objeto JSON')
    return body


def contexto(event):
    """tenant_id, usuario_id y rol SIEMPRE salen del Lambda Authorizer (JWT), nunca del body."""
    auth = (event.get('requestContext') or {}).get('authorizer') or {}
    ctx = {'tenant_id': auth.get('tenant_id'), 'usuario_id': auth.get('usuario_id'), 'rol': auth.get('rol')}
    if not all(ctx.values()) or ctx['rol'] not in ROLES:
        raise ErrorApi(401, 'NO_AUTORIZADO', 'El token no trae tenant_id, usuario_id o rol validos')
    return ctx


def exigir_rol(ctx, *roles):
    if ctx['rol'] not in roles:
        raise ErrorApi(403, 'PROHIBIDO', f"El rol {ctx['rol']} no puede realizar esta accion")


def path_param(event, nombre):
    valor = (event.get('pathParameters') or {}).get(nombre)
    if not valor:
        raise ErrorApi(400, 'VALIDACION', f'Falta el parametro {nombre} en la ruta')
    return valor


def query_params(event):
    return event.get('queryStringParameters') or {}


def texto(body, campo, minimo=1, maximo=500, requerido=True):
    valor = body.get(campo)
    if valor is None:
        if requerido:
            raise ErrorApi(400, 'VALIDACION', f'Falta el campo {campo}')
        return None
    if not isinstance(valor, str) or not minimo <= len(valor.strip()) <= maximo:
        raise ErrorApi(400, 'VALIDACION', f'{campo} debe ser texto de {minimo} a {maximo} caracteres')
    return valor.strip()


def opcion(valor, campo, permitidos):
    if valor not in permitidos:
        raise ErrorApi(400, 'VALIDACION', f"{campo} debe ser uno de: {', '.join(permitidos)}")
    return valor


# ---------------------------------------------------------------- paginacion (limite / siguiente)
def leer_paginacion(q, particion):
    """particion = claves que DEBE tener el token 'siguiente' (evita usar un token de otro tenant)."""
    try:
        limite = int(q.get('limite', 20))
    except (TypeError, ValueError):
        raise ErrorApi(400, 'VALIDACION', 'limite debe ser un numero')
    limite = max(1, min(limite, 100))
    inicio = None
    if q.get('siguiente'):
        token = q['siguiente']
        try:
            inicio = json.loads(base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode())
        except ValueError:
            raise ErrorApi(400, 'VALIDACION', 'El token siguiente no es valido')
        if not isinstance(inicio, dict) or any(inicio.get(k) != v for k, v in particion.items()):
            raise ErrorApi(400, 'VALIDACION', 'El token siguiente no corresponde a tu tenant')
    return limite, inicio


def paginacion(limite, resultado):
    lek = resultado.get('LastEvaluatedKey')
    siguiente = None
    if lek:
        siguiente = base64.urlsafe_b64encode(json.dumps(lek, default=_a_json).encode()).decode().rstrip('=')
    return {'limite': limite, 'siguiente': siguiente}


def consultar(tabla, params):
    try:
        return tabla.query(**params)
    except ClientError as e:
        if e.response['Error']['Code'] == 'ValidationException' and 'ExclusiveStartKey' in params:
            raise ErrorApi(400, 'VALIDACION', 'El token siguiente no corresponde a este filtro')
        raise


# ---------------------------------------------------------------- DynamoDB
def siguiente_numero(tenant_id, entidad):
    """Contador atomico por tenant (inc-acme-010001, inc-acme-010002, ...)."""
    r = TABLA_CONTADORES.update_item(
        Key={'tenant_id': tenant_id, 'entidad': entidad},
        UpdateExpression='ADD valor :uno',
        ExpressionAttributeValues={':uno': 1},
        ReturnValues='UPDATED_NEW',
    )
    return int(r['Attributes']['valor'])


def actualizar(tabla, clave, cambios, quitar=(), condicion=None):
    """UPDATE con SET/REMOVE. condicion={'estado': 'NUEVO'} = solo actualiza si nadie lo cambio antes."""
    nombres, valores, partes = {}, {}, []
    for i, (campo, valor) in enumerate(cambios.items()):
        nombres[f'#s{i}'] = campo
        valores[f':s{i}'] = valor
        partes.append(f'#s{i} = :s{i}')
    expresion = 'SET ' + ', '.join(partes)
    if quitar:
        for i, campo in enumerate(quitar):
            nombres[f'#r{i}'] = campo
        expresion += ' REMOVE ' + ', '.join(f'#r{i}' for i in range(len(quitar)))
    params = {
        'Key': clave,
        'UpdateExpression': expresion,
        'ExpressionAttributeNames': nombres,
        'ExpressionAttributeValues': valores,
        'ReturnValues': 'ALL_NEW',
    }
    if condicion:
        condiciones = []
        for i, (campo, valor) in enumerate(condicion.items()):
            nombres[f'#c{i}'] = campo
            valores[f':c{i}'] = valor
            condiciones.append(f'#c{i} = :c{i}')
        params['ConditionExpression'] = ' AND '.join(condiciones)
    try:
        return tabla.update_item(**params)['Attributes']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ErrorApi(409, 'CONFLICTO', 'El registro cambio mientras lo editabas, vuelve a intentarlo')
        raise


# ---------------------------------------------------------------- eventos (SNS)
def publicar_evento(evento, tenant_id, data):
    """Publica el sobre de evento del contrato. MessageAttributes 'evento' = lo que usan las FilterPolicy."""
    if sns is None:  # EVENTS_ENABLED=false: no-op, nunca rompe el CRUD
        log('INFO', accion='evento_omitido', motivo='EVENTS_ENABLED=false', evento=evento, tenant_id=tenant_id)
        return
    sobre = {
        'evento': evento,
        'version': '1.0',
        'tenant_id': tenant_id,
        'origen': 'incidentes',
        'timestamp': iso(ahora_dt()),
        'data': data,
    }
    try:
        r = sns.publish(
            TopicArn=SNS_EVENTOS_ARN,
            Subject=evento,
            Message=json.dumps(sobre, default=_a_json, ensure_ascii=False),
            MessageAttributes={
                'evento': {'DataType': 'String', 'StringValue': evento},
                'tenant_id': {'DataType': 'String', 'StringValue': tenant_id},
            },
        )
        log('INFO', accion='evento_publicado', evento=evento, tenant_id=tenant_id, message_id=r['MessageId'])
    except Exception as e:  # noqa: BLE001  (el registro ya se guardo; no se rompe la respuesta)
        log('ERROR', accion='evento_no_publicado', evento=evento, tenant_id=tenant_id, error=repr(e))


def leer_sobre(record):
    """Mensaje SQS -> sobre de evento. Soporta entrega 'raw' y tambien el formato SNS (por si acaso)."""
    cuerpo = json.loads(record['body'], parse_float=Decimal)
    if isinstance(cuerpo, dict) and cuerpo.get('Type') == 'Notification' and 'Message' in cuerpo:
        cuerpo = json.loads(cuerpo['Message'], parse_float=Decimal)
    if not isinstance(cuerpo, dict) or not cuerpo.get('evento') or not cuerpo.get('tenant_id'):
        raise ValueError('Mensaje sin evento o tenant_id')
    return cuerpo


def procesar_lote(event, procesar):
    """Procesa un lote SQS. Los que fallan se devuelven en batchItemFailures (reintento y luego DLQ)."""
    fallos = []
    for record in event.get('Records', []):
        try:
            procesar(leer_sobre(record), record)
        except Exception as e:  # noqa: BLE001
            log('ERROR', accion='mensaje_fallido', message_id=record.get('messageId'), error=repr(e))
            fallos.append({'itemIdentifier': record['messageId']})
    return {'batchItemFailures': fallos}
