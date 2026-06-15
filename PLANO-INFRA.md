# Plano de implementação — ALB + ACM + CloudFront na frente do ECS

> Conta **<ACCOUNT_ID>** · região **us-east-1** · sem domínio próprio.
> Objetivo: fechar o mínimo da arquitetura de referência (ALB :80/:443, ACM, CloudFront)
> sobre o ECS/ECR/S3/IAM/CloudWatch que já existem.
> Link final da aplicação = endpoint `https://xxxx.cloudfront.net`.

## Variáveis (valores reais já descobertos)

```bash
export RG=us-east-1
export ACCT=<ACCOUNT_ID>
export VPC=<VPC_ID>
export SUB_A=<SUBNET_A>   # us-east-1a (pública)
export SUB_D=<SUBNET_D>   # us-east-1d (pública)
export ECS_SG=<SECURITY_GROUP_ID>      # b3insight-sg (SG da task Fargate)
export CLUSTER=b3insight-cluster
export SERVICE=b3insight-dash-svc
export TASKDEF=b3insight-dash           # container "dashboard", porta 5050
export CONTAINER=dashboard
export PORT=5050
export HEALTH=/status                   # já retorna HTTP 200
```

## Passo 1 — Security Group do ALB (libera 80/443)

```bash
export ALB_SG=$(aws ec2 create-security-group --group-name b3insight-alb-sg \
  --description "ALB b3insight 80/443" --vpc-id $VPC --region $RG \
  --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id $ALB_SG \
  --protocol tcp --port 80  --cidr 0.0.0.0/0 --region $RG
aws ec2 authorize-security-group-ingress --group-id $ALB_SG \
  --protocol tcp --port 443 --cidr 0.0.0.0/0 --region $RG
# permitir ALB -> task ECS na 5050
aws ec2 authorize-security-group-ingress --group-id $ECS_SG \
  --protocol tcp --port $PORT --source-group $ALB_SG --region $RG
echo "ALB_SG=$ALB_SG"
```

## Passo 2 — Target Group (type ip, p/ Fargate)

```bash
export TG=$(aws elbv2 create-target-group --name b3insight-tg \
  --protocol HTTP --port $PORT --vpc-id $VPC --target-type ip \
  --health-check-protocol HTTP --health-check-path $HEALTH \
  --matcher HttpCode=200 --region $RG \
  --query "TargetGroups[0].TargetGroupArn" --output text)
echo "TG=$TG"
```

## Passo 3 — Certificado self-signed importado no ACM (sem domínio)

```bash
openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /tmp/alb.key -out /tmp/alb.crt -subj "/CN=b3insight.internal"
export CERT=$(aws acm import-certificate \
  --certificate fileb:///tmp/alb.crt --private-key fileb:///tmp/alb.key \
  --region $RG --query CertificateArn --output text)
echo "CERT=$CERT"
```

## Passo 4 — ALB internet-facing (2 AZs)

```bash
export ALB=$(aws elbv2 create-load-balancer --name b3insight-alb \
  --subnets $SUB_A $SUB_D --security-groups $ALB_SG \
  --scheme internet-facing --type application --region $RG \
  --query "LoadBalancers[0].LoadBalancerArn" --output text)
export ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB \
  --region $RG --query "LoadBalancers[0].DNSName" --output text)
echo "ALB=$ALB"; echo "ALB_DNS=$ALB_DNS"
```

## Passo 5 — Listeners :80 e :443

```bash
aws elbv2 create-listener --load-balancer-arn $ALB --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG --region $RG
aws elbv2 create-listener --load-balancer-arn $ALB --protocol HTTPS --port 443 \
  --certificates CertificateArn=$CERT \
  --default-actions Type=forward,TargetGroupArn=$TG --region $RG
```

## Passo 6 — Recriar o serviço ECS ligado ao Target Group

> Serviço Fargate existente **não** aceita ganhar `loadBalancers` num update.
> Recriamos apontando pro TG (mesma task def, mesma rede).

```bash
aws ecs update-service --cluster $CLUSTER --service $SERVICE \
  --desired-count 0 --region $RG
aws ecs delete-service --cluster $CLUSTER --service $SERVICE --force --region $RG
# espera sumir
aws ecs wait services-inactive --cluster $CLUSTER --services $SERVICE --region $RG

aws ecs create-service --cluster $CLUSTER --service-name $SERVICE \
  --task-definition $TASKDEF --desired-count 1 --launch-type FARGATE \
  --health-check-grace-period-seconds 60 \
  --load-balancers "targetGroupArn=$TG,containerName=$CONTAINER,containerPort=$PORT" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUB_A,$SUB_D],securityGroups=[$ECS_SG],assignPublicIp=ENABLED}" \
  --region $RG
```

## Passo 7 — Esperar target ficar healthy e testar o ALB

```bash
for i in $(seq 1 20); do
  ST=$(aws elbv2 describe-target-health --target-group-arn $TG --region $RG \
    --query "TargetHealthDescriptions[0].TargetHealth.State" --output text 2>/dev/null)
  echo "[$i] target: $ST"; [ "$ST" = "healthy" ] && break; sleep 15
done
curl -s -o /dev/null -w "ALB :80 -> HTTP %{http_code}\n" http://$ALB_DNS/status
```

## Passo 8 — CloudFront na frente (HTTPS público grátis)

> Origem = ALB por HTTP :80. Viewer = redirect-to-HTTPS com cert default `*.cloudfront.net`.
> Cache desligado (managed policy CachingDisabled) por ser dashboard/API dinâmica.

```bash
cat > /tmp/cf.json <<EOF
{
  "CallerReference": "b3insight-$(date +%s)",
  "Comment": "b3insight CDN",
  "Enabled": true,
  "Origins": {"Quantity":1,"Items":[{
    "Id":"alb-origin","DomainName":"$ALB_DNS",
    "CustomOriginConfig":{"HTTPPort":80,"HTTPSPort":443,
      "OriginProtocolPolicy":"http-only",
      "OriginSslProtocols":{"Quantity":1,"Items":["TLSv1.2"]}}
  }]},
  "DefaultCacheBehavior":{
    "TargetOriginId":"alb-origin",
    "ViewerProtocolPolicy":"redirect-to-https",
    "AllowedMethods":{"Quantity":7,
      "Items":["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
      "CachedMethods":{"Quantity":2,"Items":["GET","HEAD"]}},
    "CachePolicyId":"4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  }
}
EOF
export CF_DOMAIN=$(aws cloudfront create-distribution \
  --distribution-config file:///tmp/cf.json \
  --query "Distribution.DomainName" --output text)
echo "LINK FINAL: https://$CF_DOMAIN"
```

CloudFront leva ~5–15 min pra propagar (status `Deployed`). Depois:

```bash
curl -s -o /dev/null -w "CloudFront -> HTTP %{http_code}\n" https://$CF_DOMAIN/status
curl -s -o /dev/null -w "CloudFront /modelo -> HTTP %{http_code}\n" https://$CF_DOMAIN/modelo
```

## Teardown (acrescentar ao DEPLOY.md)

```bash
aws cloudfront ...           # disable + delete distribution
aws elbv2 delete-listener ... ; aws elbv2 delete-load-balancer --load-balancer-arn $ALB
aws elbv2 delete-target-group --target-group-arn $TG
aws acm delete-certificate --certificate-arn $CERT
aws ec2 delete-security-group --group-id $ALB_SG
```
