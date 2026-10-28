from broker import connect, declare_topology, EXCHANGE, QUEUE


def main():
    connection = connect()
    try:
        declare_topology(connection.channel())
        print(f"READY exchange={EXCHANGE} queue={QUEUE}")
    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    main()
