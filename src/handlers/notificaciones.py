"""GET /notificaciones: campana del front. La tabla la llena el consumidor de q-notificaciones."""
from boto3.dynamodb.conditions import Key

from src.common import TABLA_NOTIFICACIONES, api, consultar, contexto, leer_paginacion, ok, paginacion, query_params


@api
def listar(event, context):
    ctx = contexto(event)
    limite, inicio = leer_paginacion(query_params(event), {'tenant_id': ctx['tenant_id']})
    params = {
        'KeyConditionExpression': Key('tenant_id').eq(ctx['tenant_id']),
        'ScanIndexForward': False,  # SK = creado_en#evento_id -> mas recientes primero
        'Limit': limite,
    }
    if inicio:
        params['ExclusiveStartKey'] = inicio
    r = consultar(TABLA_NOTIFICACIONES, params)
    return ok(r['Items'], paginacion=paginacion(limite, r))
