pipeline {
    agent { label 'docker' }

    options {
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 15, unit: 'MINUTES')
    }

    parameters {
        string(name: 'ENV_CREDENTIALS_ID', defaultValue: '',
               description: '선택: GATEWAY_API_KEY, GATEWAY_REDIS_URL을 담은 Jenkins Secret file credential ID')
    }

    environment {
        COMPOSE_PROJECT_NAME = 'llm-gateway-dev'
        GATEWAY_IMAGE = "llm-gateway:jenkins-${BUILD_NUMBER}"
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Check Docker') {
            steps {
                sh '''
                    set -eu
                    docker info > /dev/null
                    docker compose version
                    docker network inspect network_dev > /dev/null
                    docker compose -f docker-compose.jenkins.yml config --quiet
                '''
            }
        }

        stage('Build') {
            steps {
                sh 'docker compose -f docker-compose.jenkins.yml build --pull gateway'
            }
        }

        stage('Deploy') {
            steps {
                script {
                    def deploy = {
                        sh '''
                            set -eu
                            docker compose -f docker-compose.jenkins.yml up -d --no-build --wait --wait-timeout 180 gateway
                            docker compose -f docker-compose.jenkins.yml exec -T gateway python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:35000/readyz', timeout=120)"
                        '''
                    }
                    if (params.ENV_CREDENTIALS_ID.trim()) {
                        withCredentials([file(credentialsId: params.ENV_CREDENTIALS_ID.trim(), variable: 'GATEWAY_ENV_FILE')]) {
                            deploy()
                        }
                    } else {
                        deploy()
                    }
                }
            }
        }
    }

    post {
        failure {
            sh 'docker compose -f docker-compose.jenkins.yml logs --tail=100 gateway || true'
        }
    }
}
