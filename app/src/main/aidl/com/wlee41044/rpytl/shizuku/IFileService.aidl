package com.wlee41044.rpytl.shizuku;

// Shizuku UserService (shell 권한 프로세스)에서 Android/data 아래 파일을 다루는 인터페이스.
interface IFileService {
    // Shizuku 가 서비스를 끝낼 때 부르는 예약 메서드
    void destroy() = 16777114;

    int uid() = 1;

    // dir 아래 파일 목록(재귀). JSON 배열 [{"p": 상대경로, "s": 크기}], 폴더가 없으면 "[]"
    String list(String dir) = 2;

    boolean exists(String path) = 3;

    void mkdirs(String dir) = 4;

    // src 내용을 path 에 쓴다 (임시 파일에 쓰고 이름 바꾸기)
    void write(String path, in ParcelFileDescriptor src) = 5;

    boolean remove(String path) = 6;

    ParcelFileDescriptor openRead(String path) = 7;
}
