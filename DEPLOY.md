# Deploy AWS — Radar B3 (conta própria)

> Provisionado em 14/jun/2026 na conta AWS **<ACCOUNT_ID>** (`cloud-aula`).
> Região: **us-east-1**. Substitui a conta do Pedro (<OLD_ACCOUNT_ID>) que expirou.

## URLs do produto

| O quê | URL |
|---|---|
| **Dashboard (EC2 — IP fixo)** | http://<EC2_PUBLIC_IP>:5050 |
| Página do modelo ML | http://<EC2_PUBLIC_IP>:5050/modelo |
| **Dashboard (ECS/Fargate)** | http://<ECS_TASK_PUBLIC_IP>:5050  *(IP muda se a task reiniciar — ver abaixo)* |

## Arquitetura

```
B3/CVM + Yahoo + Fundamentus   →  WORKER (EC2)  →  S3 (Medallion)  →  DASHBOARD  →  web
  (fontes de dados)               ingestão          bronze/silver/gold   Flask :5050
                                  de hora em hora                         (EC2 e ECS)
```

- **Ingestão** (`scripts/worker.py`, systemd `radar-b3`): de hora em hora coleta
  cotações (Yahoo) + fatos da CVM + PDFs; 1×/dia coleta fundamentos (fundamentus).
- **Storage**: S3 `b3insight-data` em camadas bronze → silver → gold.
- **Compute/Apresentação**: dashboard Flask roda em DOIS lugares — EC2 (systemd
  `radar-b3-dash`) e ECS/Fargate (serviço `b3insight-dash-svc`). Demonstra a
  migração EC2 → ECS. Ambos leem do S3 via IAM Role (sem chaves no código).

## Recursos AWS criados

| Recurso | Nome / ID |
|---|---|
| S3 bucket | `b3insight-data` |
| EC2 | `<INSTANCE_ID>` (t3.micro) — IP fixo `<EC2_PUBLIC_IP>` |
| Elastic IP | `<EC2_PUBLIC_IP>` (alloc `<EIP_ALLOC_ID>`) |
| IAM role EC2 | `b3insight-ec2-role` (S3 + ECR) |
| Key pair | `b3insight-key` (`.pem` em `ML- b3insigh/b3insight-key.pem`) |
| Security group | `b3insight-sg` (`<SECURITY_GROUP_ID>`) — portas 22 e 5050 |
| ECR repo | `<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/b3insight` |
| ECS cluster | `b3insight-cluster` (Fargate) |
| ECS service | `b3insight-dash-svc` (task `b3insight-dash`) |
| Roles ECS | `ecsTaskExecutionRole`, `b3insight-ecs-task-role` |
| Log group | `/ecs/b3insight` |

## Comandos úteis

```bash
# SSH na EC2
ssh -i "ML- b3insigh/b3insight-key.pem" ubuntu@<EC2_PUBLIC_IP>

# Logs do worker (ingestão)
ssh ... 'sudo journalctl -u radar-b3 -f'

# Ver o S3
aws s3 ls s3://b3insight-data/ --recursive --region us-east-1

# Pegar o IP atual da task ECS (caso tenha reiniciado)
TASK=$(aws ecs list-tasks --cluster b3insight-cluster --region us-east-1 --query "taskArns[0]" --output text)
ENI=$(aws ecs describe-tasks --cluster b3insight-cluster --tasks $TASK --region us-east-1 --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value" --output text)
aws ec2 describe-network-interfaces --network-interface-ids $ENI --region us-east-1 --query "NetworkInterfaces[0].Association.PublicIp" --output text

# Redeploy de código novo (EC2)
cd arquivo-clonado && tar --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='dados' -czf /tmp/app.tar.gz .
aws s3 cp /tmp/app.tar.gz s3://b3insight-data/deploy/app.tar.gz --region us-east-1
ssh ... 'cd radar-b3 && aws s3 cp s3://b3insight-data/deploy/app.tar.gz ~/app.tar.gz --region us-east-1 && tar -xzf ~/app.tar.gz && sudo systemctl restart radar-b3 radar-b3-dash'
```

## ⚠️ TEARDOWN (rodar DEPOIS da entrega, pra não gerar cobrança)

```bash
RG=us-east-1
# 1. Parar/remover ECS
aws ecs update-service --cluster b3insight-cluster --service b3insight-dash-svc --desired-count 0 --region $RG
aws ecs delete-service --cluster b3insight-cluster --service b3insight-dash-svc --force --region $RG
aws ecs delete-cluster --cluster b3insight-cluster --region $RG
# 2. Terminar EC2
aws ec2 terminate-instances --instance-ids <INSTANCE_ID> --region $RG
# 3. Liberar Elastic IP (senão cobra quando não associado)
aws ec2 release-address --allocation-id <EIP_ALLOC_ID> --region $RG
# 4. (opcional) apagar bucket e ECR
# aws s3 rb s3://b3insight-data --force
# aws ecr delete-repository --repository-name b3insight --force --region $RG
```

## Notas / limitações conscientes

- Security group abre 22 e 5050 para `0.0.0.0/0` — ok para demo de conta
  descartável; em produção restringir por IP.
- ECS sem load balancer: o IP público da task muda se ela reiniciar. Para URL
  fixa no ECS seria preciso um ALB (não é free tier).
- Bucket em us-east-1, EC2/ECS na mesma região (compute junto do storage).
