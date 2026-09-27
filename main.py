import math
import random

from weather import WeatherError, consultar_previsao, validar_data_previsao


def probabilidade_simples():
    try:
        casos_favoraveis = int(input("Casos favoráveis: "))
        casos_possiveis = int(input("Casos possíveis: "))
    except ValueError:
        print("Erro: digite números inteiros.")
        return

    if casos_favoraveis < 0 or casos_possiveis <= 0:
        print("Erro: valores inválidos.")
        return

    if casos_favoraveis > casos_possiveis:
        print("Erro: casos favoráveis não podem ser maiores que casos possíveis.")
        return

    probabilidade = casos_favoraveis / casos_possiveis * 100
    print(f"Probabilidade: {probabilidade:.2f}%")


def permutacao():
    try:
        n = int(input("Digite o valor de n: "))
    except ValueError:
        print("Erro: digite um número inteiro.")
        return

    if n < 0:
        print("Erro: n não pode ser negativo.")
        return

    print(f"{n}! = {math.factorial(n)}")


def combinacao():
    try:
        n = int(input("Digite o valor de n: "))
        r = int(input("Digite o valor de r: "))
    except ValueError:
        print("Erro: digite números inteiros.")
        return

    if n < 0 or r < 0 or r > n:
        print("Valores inválidos.")
        return

    print(f"C({n}, {r}) = {math.comb(n, r)}")


def arranjo():
    try:
        n = int(input("Digite o valor de n: "))
        r = int(input("Digite o valor de r: "))
    except ValueError:
        print("Erro: digite números inteiros.")
        return

    if n < 0 or r < 0 or r > n:
        print("Valores inválidos.")
        return

    resultado = math.factorial(n) // math.factorial(n - r)
    print(f"A({n}, {r}) = {resultado}")


def simular_dado():
    resultado = random.randint(1, 6)
    print(f"O dado caiu no número: {resultado}")


def calculadora():
    while True:
        print("\n==============================")
        print("   CALCULADORA DE PROBABILIDADE")
        print("==============================")
        print("1 - Probabilidade simples")
        print("2 - Permutação")
        print("3 - Combinação")
        print("4 - Arranjo")
        print("5 - Simular lançamento de dado")
        print("6 - Sair")

        try:
            opcao = input("\nEscolha uma opção: ").strip()
        except EOFError:
            print("\nEntrada encerrada. Programa encerrado.")
            break

        if opcao == "1":
            probabilidade_simples()

        elif opcao == "2":
            permutacao()

        elif opcao == "3":
            combinacao()

        elif opcao == "4":
            arranjo()

        elif opcao == "5":
            simular_dado()

        elif opcao == "6":
            print("Programa encerrado.")
            break

        else:
            print("Opção inválida.")


def aplicativo_previsao():
    cidade = input("Digite a cidade: ").strip()
    if not cidade:
        print("Erro: informe uma cidade.")
        return

    data_texto = input("Digite a data da previsão (AAAA-MM-DD): ").strip()
    try:
        data = validar_data_previsao(data_texto)
        previsao = consultar_previsao(cidade, data)
    except (ValueError, WeatherError) as erro:
        print(f"Erro: {erro}")
        return

    print("\n==============================")
    print(f" PREVISÃO DO TEMPO — {previsao['cidade']}")
    print(f" Data: {previsao['data']}")
    print("==============================")
    print(f"Condição: {previsao['condicao']}")
    print(f"Temperatura mínima: {previsao['temperatura_minima']}")
    print(f"Temperatura máxima: {previsao['temperatura_maxima']}")
    print(f"Probabilidade de chuva: {previsao['probabilidade_chuva']}")
    print(f"Umidade média: {previsao['umidade']}")
    print(f"Velocidade máxima do vento: {previsao['vento']}")


def menu_principal():
    while True:
        print("\n==============================")
        print("       APLICATIVO")
        print("==============================")
        print("1 - Calculadora de probabilidade")
        print("2 - Previsão do tempo")
        print("3 - Sair")

        try:
            opcao = input("\nEscolha uma opção: ").strip()
        except EOFError:
            print("\nEntrada encerrada. Programa encerrado.")
            break

        if opcao == "1":
            calculadora()
        elif opcao == "2":
            aplicativo_previsao()
        elif opcao == "3":
            print("Programa encerrado.")
            break
        else:
            print("Opção inválida.")


if __name__ == "__main__":
    menu_principal()
